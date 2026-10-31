"""
Constant Node - Generates solid color, checker, or gradient images
at a user-specified resolution.

Like NUKE's Constant node, used to set the composite format/resolution
or provide a colored background / test-pattern layer.

Supported modes:
  - solid:    Flat fill with user-specified RGBA (HDR-safe, values > 1.0).
  - checker:  Two-color checkerboard pattern with configurable square size.
  - gradient: Linear gradient between two colors (horizontal, vertical, diagonal).
  - radial:   Radial gradient from center outward between two colors.

All color parameters accept HDR float values (>= 0.0, unclamped).
"""

import numpy as np
import moderngl
from typing import Any, Dict, Optional, Set, Tuple
from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace
from src.shared.color.color_conversion import (
    linear_rgb_to_hex,
    srgb_to_linear,
)
from src.features.edit.core.composite_graph_data import (
    CONSTANT_COLOR_SPACE_PARAM,
)


class ConstantNode(Node):
    """Generates a solid / checker / gradient / radial texture at a specified resolution."""

    node_type = "constant"
    node_title = "Constant"
    i18n_title_key = "nodes.constant.title"
    header_color = "#6b5b95"  # Purple

    # Metadata
    SEARCH_ALIASES = ["constant", "bg", "background", "solid", "checker", "gradient", "常量", "背景", "纯色", "渐变"]
    DESCRIPTION = "生成纯色、棋盘格或渐变图像"
    TAGS = ["input", "source", "generator"]

    PARAM_DEFS = [
        # ── Dimensions ──
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        # ── Mode ──
        ParamDef("mode", "option", "solid",
                 labels=["solid", "checker", "gradient", "radial"]),
        # Canonical atomic color values.  The channel fields below remain as
        # private render mirrors for the existing shader/thumbnail helpers;
        # animation and all new UI writes use these two RGBA values directly.
        ParamDef("color", "color", (0.0, 0.0, 0.0, 1.0), hidden=True,
                 allow_expression=False),
        ParamDef("color2", "color", (1.0, 1.0, 1.0, 1.0), hidden=True,
                 allow_expression=False),
        # ── Color A (primary color / top-left / center) ──
        ParamDef("color_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("color_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("color_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("color_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # ── Color B (secondary color for checker/gradient/radial) ──
        ParamDef("color2_r", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("color2_g", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("color2_b", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("color2_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # ── Checker settings ──
        # The stored value is the actual square size in pixels (4-400). The
        # properties panel presents this on a friendlier 1-100 UI scale that
        # maps linearly (render = ui * 4); see _create_constant_checker_row.
        ParamDef("checker_size", "int", 64, min_val=2, max_val=400, step=1),
        # ── Gradient settings ──
        # Free start/end points (normalized 0-1, may extend beyond) replace the
        # legacy 4-direction option so the gradient can be positioned by the
        # on-screen handles in the viewer. Defaults reproduce the old
        # "horizontal" preset (left -> right).
        ParamDef("start_x", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("start_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("end_x", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("end_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        # ── Radial settings ──
        ParamDef("center_x", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("center_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        ParamDef("radial_radius", "float", 0.5, min_val=0.001, max_val=2.0, step=0.01),
        # ── Feather / contrast (gradient + radial) ──
        # -1 = soft/blurry (lower contrast), 0 = linear (neutral),
        # +1 = high contrast (wider solid regions). See apply_feather in
        # _CONSTANT_GRADIENT_FRAGMENT for the exact mapping.
        ParamDef("feather", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
        # ── Output metadata ──
        ParamDef("format_name", "str", "Constant"),
        # NOTE: no output_color_space here. The Constant generates pixels in
        # linear space; any output color-space conversion is the Output node's
        # responsibility (it has its own output_color_space param that actually
        # controls the display transform). A per-source color-space tag would
        # only be metadata that nothing consumes, so it is intentionally absent.
        # ── Hidden UI-only parameters (not serialized for rendering) ──
        ParamDef("size_preset", "str", "HD 1080", hidden=True),
        ParamDef("color_hex", "str", "", hidden=True),
        ParamDef("color2_hex", "str", "", hidden=True),
        ParamDef(CONSTANT_COLOR_SPACE_PARAM, "str", "linear", hidden=True),
    ]

    # AUD-6504: dimension upper bound derived from PARAM_DEFS (never a
    # hardcoded copy that could drift).  from_dict loads parameters without
    # range validation, and _do_evaluate allocates the GPU texture directly
    # from width/height — an unclamped 100000x100000 would ask the GL for a
    # ~160GB RGBA32F texture.
    _DIMENSION_MAX = max(
        (int(pdef.max_val) for pdef in PARAM_DEFS
         if pdef.name in ("width", "height") and pdef.max_val is not None),
        default=16384,
    )

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            # Dimensions
            "width": 1920,
            "height": 1080,
            # Mode
            "mode": "solid",
            # Canonical atomic colors used by animation and UI.
            "color": (0.0, 0.0, 0.0, 1.0),
            "color2": (1.0, 1.0, 1.0, 1.0),
            # Color A
            "color_r": 0.0,
            "color_g": 0.0,
            "color_b": 0.0,
            "color_a": 1.0,
            # Color B
            "color2_r": 1.0,
            "color2_g": 1.0,
            "color2_b": 1.0,
            "color2_a": 1.0,
            # Checker
            "checker_size": 64,
            # Gradient (free start/end points, default = horizontal L->R)
            "start_x": 0.0,
            "start_y": 0.5,
            "end_x": 1.0,
            "end_y": 0.5,
            # Radial
            "center_x": 0.5,
            "center_y": 0.5,
            "radial_radius": 0.5,
            # Feather (shared by gradient + radial)
            "feather": 0.0,
            # Output metadata
            "format_name": "Constant",
            # Hidden UI-only
            "size_preset": "HD 1080",
            "color_hex": "",
            "color2_hex": "",
            CONSTANT_COLOR_SPACE_PARAM: "linear",
        }
        self._gpu_texture: Optional[moderngl.Texture] = None
        self._last_size: Tuple[int, int] = (0, 0)
        # GPU resources for the gradient/radial shader path. These are compiled
        # once and reused across renders (the program and quad geometry are
        # resolution-independent). The render FBO is tied to a specific output
        # texture, so it must be released together with self._gpu_texture.
        self._gradient_prog: Optional[moderngl.Program] = None
        self._render_fbo: Optional[moderngl.Framebuffer] = None
        self._quad_vbo: Optional[moderngl.Buffer] = None
        # Signature of the params that affect the rendered pixels. When this
        # changes (or the texture is missing) we regenerate. This is more
        # robust than comparing param_revision, which can race with the
        # render-node sync path and cause mode changes to be skipped.
        self._last_content_sig: Optional[tuple] = None
        self._revision: int = 0  # bumped on each re-render
        # Set True whenever the GL texture is released and recreated. The
        # evaluator reads & clears this to invalidate stale cache entries
        # (whose TextureResult still references the now-released glo).
        self._cache_invalidated_needed: bool = False

    @staticmethod
    def normalize_color_parameters(
        parameters: Dict[str, object], *, legacy_srgb: bool = False,
    ) -> Dict[str, object]:
        """Return Constant parameters with scene-linear RGB channels.

        Before the color-management fix, the properties picker wrote sRGB
        channel values directly into Constant.  ``legacy_srgb`` converts only
        that old representation; values above 1.0 are retained as HDR values.
        """
        has_color = "color" in parameters
        has_color2 = "color2" in parameters
        normalized = dict(parameters or {})
        if not legacy_srgb:
            normalized.setdefault(CONSTANT_COLOR_SPACE_PARAM, "linear")
            return normalized

        for key in (
            "color_r", "color_g", "color_b",
            "color2_r", "color2_g", "color2_b",
        ):
            if key not in normalized:
                continue
            try:
                value = float(normalized[key])
            except (TypeError, ValueError):
                continue
            # The old UI only represented SDR values.  Keep explicit HDR
            # values untouched instead of applying an SDR transfer to them.
            normalized[key] = srgb_to_linear(value) if 0.0 <= value <= 1.0 else value
        normalized[CONSTANT_COLOR_SPACE_PARAM] = "linear"
        if not has_color:
            normalized["color"] = (
                normalized.get("color_r", 0.0), normalized.get("color_g", 0.0),
                normalized.get("color_b", 0.0), normalized.get("color_a", 1.0))
        if not has_color2:
            normalized["color2"] = (
                normalized.get("color2_r", 1.0), normalized.get("color2_g", 1.0),
                normalized.get("color2_b", 1.0), normalized.get("color2_a", 1.0))
        return normalized

    @classmethod
    def from_dict(cls, data: dict) -> "ConstantNode":
        """Load Constant data and migrate pre-color-management projects."""
        loaded_params = data.get("parameters", {}) or {}
        legacy_srgb = loaded_params.get(CONSTANT_COLOR_SPACE_PARAM) != "linear"
        node = super().from_dict(data)
        if legacy_srgb:
            node.parameters = cls.normalize_color_parameters(
                node.parameters, legacy_srgb=True)
            if "color" not in loaded_params:
                node.parameters["color"] = (
                    node.parameters.get("color_r", 0.0),
                    node.parameters.get("color_g", 0.0),
                    node.parameters.get("color_b", 0.0),
                    node.parameters.get("color_a", 1.0))
            if "color2" not in loaded_params:
                node.parameters["color2"] = (
                    node.parameters.get("color2_r", 1.0),
                    node.parameters.get("color2_g", 1.0),
                    node.parameters.get("color2_b", 1.0),
                    node.parameters.get("color2_a", 1.0))
        else:
            node.parameters.setdefault(CONSTANT_COLOR_SPACE_PARAM, "linear")
            if "color" not in loaded_params:
                node.parameters["color"] = (
                    node.parameters.get("color_r", 0.0),
                    node.parameters.get("color_g", 0.0),
                    node.parameters.get("color_b", 0.0),
                    node.parameters.get("color_a", 1.0))
            if "color2" not in loaded_params:
                node.parameters["color2"] = (
                    node.parameters.get("color2_r", 1.0),
                    node.parameters.get("color2_g", 1.0),
                    node.parameters.get("color2_b", 1.0),
                    node.parameters.get("color2_a", 1.0))
        node._sync_color_channel_mirrors()
        return node

    @staticmethod
    def _color_value(parameters: Dict[str, object], key: str,
                     fallback: Tuple[float, float, float, float]) -> Tuple[float, ...]:
        value = parameters.get(key)
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                return tuple(float(component) for component in value)
            except (TypeError, ValueError):
                pass
        channel_prefix = "color" if key == "color" else "color2"
        return tuple(float(parameters.get(f"{channel_prefix}_{channel}", default))
                     for channel, default in zip(
                         ("r", "g", "b", "a"), fallback))

    def _sync_color_channel_mirrors(self) -> None:
        """Keep legacy shader-facing channel values derived from atomic colors."""
        for key, prefix, fallback in (
                ("color", "color", (0.0, 0.0, 0.0, 1.0)),
                ("color2", "color2", (1.0, 1.0, 1.0, 1.0))):
            rgba = self._color_value(self.parameters, key, fallback)
            self.parameters[key] = rgba
            for channel, value in zip(("r", "g", "b", "a"), rgba):
                self.parameters[f"{prefix}_{channel}"] = value

    def set_parameter(self, key: str, value) -> None:
        """Write canonical colors atomically and derive channel mirrors."""
        if key in ("color", "color2"):
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                return
            rgba = tuple(float(component) for component in value)
            super().set_parameter(key, rgba)
            prefix = key
            for channel, component in zip(("r", "g", "b", "a"), rgba):
                self.parameters[f"{prefix}_{channel}"] = component
            return
        if key in {
                "color_r", "color_g", "color_b", "color_a",
                "color2_r", "color2_g", "color2_b", "color2_a"}:
            super().set_parameter(key, value)
            prefix = "color2" if key.startswith("color2_") else "color"
            rgba = list(self._color_value(self.parameters, prefix,
                                           (1.0, 1.0, 1.0, 1.0)
                                           if prefix == "color2" else
                                           (0.0, 0.0, 0.0, 1.0)))
            rgba["rgba".index(key.rsplit("_", 1)[-1])] = float(value)
            super().set_parameter(prefix, tuple(rgba))
            return
        super().set_parameter(key, value)

    # ── UI Helper Methods ──

    @staticmethod
    def mode_param_visibility(mode: str) -> Set[str]:
        """Return the set of parameter names that should be hidden for *mode*.

        Used by the properties panel to show/hide rows when the mode combo
        changes.  Always hides metadata-only params (format_name) and the
        hex-string display params (color_hex, color2_hex).
        """
        # These are never shown directly — the custom UI renders color pickers instead.
        always_hidden: Set[str] = {
            "color", "color2", "color_hex", "color2_hex", "size_preset", "format_name",
            "color_r", "color_g", "color_b", "color_a",
            "color2_r", "color2_g", "color2_b", "color2_a",
        }
        if mode == "solid":
            return always_hidden | {
                "color2_r", "color2_g", "color2_b", "color2_a",
                "checker_size",
                "start_x", "start_y", "end_x", "end_y",
                "center_x", "center_y", "radial_radius",
                "feather",
            }
        elif mode == "checker":
            return always_hidden | {
                "start_x", "start_y", "end_x", "end_y",
                "center_x", "center_y", "radial_radius",
                "feather",
            }
        elif mode == "gradient":
            # gradient uses start/end + feather; hide radial-only params.
            return always_hidden | {
                "checker_size",
                "center_x", "center_y", "radial_radius",
            }
        elif mode == "radial":
            # radial uses center/radius + feather; hide gradient-only params.
            return always_hidden | {
                "checker_size",
                "start_x", "start_y", "end_x", "end_y",
            }
        return always_hidden

    @staticmethod
    def keyframe_parameter_specs(mode: str) -> list[tuple[str, tuple[str, ...]]]:
        """Return the user-facing animatable parameters for a Constant mode.

        Colors are real atomic parameters.  The second tuple item is retained
        only as a stable return shape for callers that also list scalar rows;
        it is always empty because there are no backing channel tracks.
        """
        specs: list[tuple[str, tuple[str, ...]]] = [
            ("width", ()),
            ("height", ()),
            ("color", ()),
        ]
        if mode in ("checker", "gradient", "radial"):
            specs.append((
                "color2", (),
            ))
        if mode == "checker":
            specs.append(("checker_size", ()))
        elif mode == "gradient":
            specs.extend([
                ("start_x", ()), ("start_y", ()),
                ("end_x", ()), ("end_y", ()), ("feather", ()),
            ])
        elif mode == "radial":
            specs.extend([
                ("center_x", ()), ("center_y", ()),
                ("radial_radius", ()), ("feather", ()),
            ])
        return specs

    @staticmethod
    def rgba_to_hex(r: float, g: float, b: float) -> str:
        """Convert scene-linear RGB values to a display-referred hex string."""
        return linear_rgb_to_hex(r, g, b)

    @staticmethod
    def hex_to_rgba(hex_str: str, default_alpha: float = 1.0) -> Tuple[float, float, float, float]:
        """Convert an sRGB hex color to a scene-linear RGBA tuple.

        Supports ``#rrggbb`` and ``#rrggbbaa`` formats.
        Returns ``(0.0, 0.0, 0.0, default_alpha)`` on parse failure.
        """
        hex_str = hex_str.strip().lstrip("#")
        try:
            if len(hex_str) == 8:
                r = srgb_to_linear(int(hex_str[0:2], 16) / 255.0)
                g = srgb_to_linear(int(hex_str[2:4], 16) / 255.0)
                b = srgb_to_linear(int(hex_str[4:6], 16) / 255.0)
                a = int(hex_str[6:8], 16) / 255.0
                return (r, g, b, a)
            elif len(hex_str) == 6:
                r = srgb_to_linear(int(hex_str[0:2], 16) / 255.0)
                g = srgb_to_linear(int(hex_str[2:4], 16) / 255.0)
                b = srgb_to_linear(int(hex_str[4:6], 16) / 255.0)
                return (r, g, b, default_alpha)
        except (ValueError, IndexError):
            pass
        return (0.0, 0.0, 0.0, default_alpha)

    # ── Parameter hash for cache invalidation ──

    def _content_hash(self) -> int:
        """Hash of all parameters that affect pixel content (not metadata)."""
        return hash((
            self.parameters.get("mode", "solid"),
            self._color_value(self.parameters, "color", (0.0, 0.0, 0.0, 1.0)),
            self._color_value(self.parameters, "color2", (1.0, 1.0, 1.0, 1.0)),
            self.parameters.get("checker_size", 64),
            # gradient params
            self.parameters.get("start_x", 0.0),
            self.parameters.get("start_y", 0.5),
            self.parameters.get("end_x", 1.0),
            self.parameters.get("end_y", 0.5),
            # radial params
            self.parameters.get("center_x", 0.5),
            self.parameters.get("center_y", 0.5),
            self.parameters.get("radial_radius", 0.5),
            # shared
            self.parameters.get("feather", 0.0),
        ))

    # ── Pattern generators (numpy, CPU-side) ──

    @staticmethod
    def _make_solid(w: int, h: int,
                    r: float, g: float, b: float, a: float) -> np.ndarray:
        """Solid fill RGBA float32 array (h, w, 4)."""
        data = np.empty((h, w, 4), dtype=np.float32)
        data[..., 0] = r
        data[..., 1] = g
        data[..., 2] = b
        data[..., 3] = a
        return data

    @staticmethod
    def _make_checker(w: int, h: int,
                      r1: float, g1: float, b1: float, a1: float,
                      r2: float, g2: float, b2: float, a2: float,
                      size: int) -> np.ndarray:
        """Two-color checkerboard pattern."""
        data = np.empty((h, w, 4), dtype=np.float32)
        ys = np.arange(h, dtype=np.int32)
        xs = np.arange(w, dtype=np.int32)
        yy, xx = np.meshgrid(ys, xs, indexing="ij")
        mask = ((yy // size) + (xx // size)) % 2 == 0

        data[..., 0] = np.where(mask, r1, r2)
        data[..., 1] = np.where(mask, g1, g2)
        data[..., 2] = np.where(mask, b1, b2)
        data[..., 3] = np.where(mask, a1, a2)
        return data

    # ── GPU gradient/radial rendering ──────────────────────────────────────
    #
    # gradient and radial modes are generated on the GPU via a dedicated
    # fragment shader (_CONSTANT_GRADIENT_FRAGMENT). This keeps drag-to-
    # position handle interaction at 60fps: each param change only rebinds
    # uniforms and re-renders a fullscreen quad, instead of regenerating an
    # 8M-pixel numpy buffer on the CPU. solid/checker stay on the numpy path
    # (checker's discrete grid is not worth shader-ifying).
    #
    # The shader resources (program, quad VBO) are compiled once and cached
    # on the node; the render FBO is tied to the current output texture and
    # is therefore released alongside self._gpu_texture.

    # Fullscreen TRIANGLE_STRIP quad: (x, y, u, v) pairs covering NDC [-1,1]²
    # with UV [0,1]². Matches the layout consumed by _PASSTHROUGH_VERTEX
    # (in_vert = xy, in_uv = uv).
    _QUAD_VERTICES = np.array([
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ], dtype='f4')

    def _ensure_gradient_program(self, ctx) -> moderngl.Program:
        """Lazy-compile (and cache) the constant gradient shader program."""
        if self._gradient_prog is None:
            from src.features.composite.core.shaders import (
                _PASSTHROUGH_VERTEX, _CONSTANT_GRADIENT_FRAGMENT)
            self._gradient_prog = ctx.program(
                vertex_shader=_PASSTHROUGH_VERTEX,
                fragment_shader=_CONSTANT_GRADIENT_FRAGMENT,
            )
        return self._gradient_prog

    def _ensure_quad_vbo(self, ctx) -> moderngl.Buffer:
        """Lazy-create (and cache) the fullscreen-quad VBO."""
        if self._quad_vbo is None:
            self._quad_vbo = ctx.buffer(self._QUAD_VERTICES)
        return self._quad_vbo

    def _render_gradient_gpu(self, ctx, w: int, h: int, mode: str,
                              r1: float, g1: float, b1: float, a1: float,
                              r2: float, g2: float, b2: float, a2: float) -> None:
        """Render gradient/radial into a fresh self._gpu_texture via FBO.

        On entry self._gpu_texture must already be released/cleared; on exit
        it holds the rendered gradient. The render FBO is rebuilt for the new
        texture (FBOs are bound to a specific color attachment).
        """
        prog = self._ensure_gradient_program(ctx)
        vbo = self._ensure_quad_vbo(ctx)

        # Fresh empty output texture (self-managed — see owns_color_texture=False).
        self._gpu_texture = ctx.texture((w, h), 4, dtype='f4')
        self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Render FBO bound to that texture. Built per-render because the FBO
        # references a specific texture glo; reusing across textures is unsafe.
        self._render_fbo = ctx.framebuffer(color_attachments=[self._gpu_texture])
        self._render_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Uniforms. start/end/center/radius are normalized 0-1 (matching the
        # on-screen handle params), feather is 0-1.
        if mode == "radial":
            prog['u_mode'].value = 1
            prog['u_center'].value = (
                float(self.parameters.get("center_x", 0.5)),
                float(self.parameters.get("center_y", 0.5)),
            )
            prog['u_radius'].value = max(
                0.001, float(self.parameters.get("radial_radius", 0.5)))
            # start/end unused in radial mode but set for safety.
            prog['u_start_point'].value = (0.5, 0.5)
            prog['u_end_point'].value = (0.5, 0.5)
        else:  # gradient
            prog['u_mode'].value = 0
            prog['u_start_point'].value = (
                float(self.parameters.get("start_x", 0.0)),
                float(self.parameters.get("start_y", 0.5)),
            )
            prog['u_end_point'].value = (
                float(self.parameters.get("end_x", 1.0)),
                float(self.parameters.get("end_y", 0.5)),
            )
            prog['u_center'].value = (0.5, 0.5)
            prog['u_radius'].value = 0.5

        prog['u_color1'].value = (r1, g1, b1, a1)
        prog['u_color2'].value = (r2, g2, b2, a2)
        prog['u_feather'].value = float(self.parameters.get("feather", 0.0))
        prog['u_resolution'].value = (float(w), float(h))

        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        vao.render(moderngl.TRIANGLE_STRIP)
        vao.release()
        # Note: vbo is intentionally NOT released here — it is cached on the
        # node and reused across renders. It is freed in cleanup().

    # ── Evaluate ──

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        # Unwrap CompositeEvalContext to get the actual ModernGL context.
        # Source nodes receive CompositeEvalContext for business-level context
        # (runtime_context, timeline_clip_gpu_provider), but GL operations
        # like ctx.texture() need the real moderngl.Context.
        from src.features.composite.core.eval_context import CompositeEvalContext
        if isinstance(ctx, CompositeEvalContext):
            ctx = ctx.gl

        # AUD-6504: clamp BOTH bounds — min 1 and the PARAM_DEFS maximum —
        # so unvalidated loaded values cannot trigger oversized GPU
        # texture allocations (ctx.texture below allocates w*h*16 bytes).
        w = min(self._DIMENSION_MAX,
                max(1, int(self.parameters.get("width", 1920))))
        h = min(self._DIMENSION_MAX,
                max(1, int(self.parameters.get("height", 1080))))
        mode = self.parameters.get("mode", "solid")
        fmt_name = self.parameters.get("format_name", "Constant") or "Constant"
        # Constant always generates pixels in scene-linear space. The Output
        # node owns the display transform (its output_color_space param), so
        # there is no per-source color-space knob here — see PARAM_DEFS note.

        # Color A
        r1, g1, b1, a1 = self._color_value(
            self.parameters, "color", (0.0, 0.0, 0.0, 1.0))

        # Color B
        r2, g2, b2, a2 = self._color_value(
            self.parameters, "color2", (1.0, 1.0, 1.0, 1.0))

        # Build a content signature from the params that actually affect the
        # rendered pixels. This is race-free: it reflects the exact values
        # present in this evaluation rather than relying on param_revision
        # (which is synced separately and can be stale at this point).
        checker_size = max(2, int(self.parameters.get("checker_size", 64)))
        # gradient / radial / feather params (read once for both the content
        # signature and the GPU render below).
        start_x = float(self.parameters.get("start_x", 0.0))
        start_y = float(self.parameters.get("start_y", 0.5))
        end_x = float(self.parameters.get("end_x", 1.0))
        end_y = float(self.parameters.get("end_y", 0.5))
        center_x = float(self.parameters.get("center_x", 0.5))
        center_y = float(self.parameters.get("center_y", 0.5))
        radial_radius = float(self.parameters.get("radial_radius", 0.5))
        feather = float(self.parameters.get("feather", 0.0))
        content_sig = (
            mode, w, h,
            r1, g1, b1, a1, r2, g2, b2, a2,
            checker_size,
            start_x, start_y, end_x, end_y,
            center_x, center_y, radial_radius,
            feather,
        )
        content_changed = content_sig != self._last_content_sig

        if content_changed or self._gpu_texture is None:
            if self._gpu_texture is not None:
                self._gpu_texture.release()
                self._gpu_texture = None
                # The render FBO is bound to the (now released) texture glo,
                # so it must be released too — reusing it against a different
                # attachment is unsafe.
                if self._render_fbo is not None:
                    self._render_fbo.release()
                    self._render_fbo = None
                # The released glo may be reused by the GL driver for a new
                # texture. Stale cache entries (from previous modes/params)
                # still hold the old glo in their TextureResult, so flag that
                # the evaluator must invalidate all prior cache entries for
                # this node before storing the new one. Without this, switching
                # mode back (e.g. solid→radial→solid) can hit a stale entry
                # whose glo now points at unrelated/released data.
                self._cache_invalidated_needed = True

            # Generate pixel data based on mode.
            # gradient/radial take the GPU shader path (fast uniform updates);
            # solid/checker stay on the numpy upload path.
            if mode in ("gradient", "radial"):
                self._render_gradient_gpu(
                    ctx, w, h, mode,
                    r1, g1, b1, a1, r2, g2, b2, a2)
            else:
                if mode == "checker":
                    data = self._make_checker(w, h, r1, g1, b1, a1,
                                              r2, g2, b2, a2, checker_size)
                else:  # solid
                    data = self._make_solid(w, h, r1, g1, b1, a1)
                self._gpu_texture = ctx.texture(
                    (w, h), 4, data=data.tobytes(), dtype='f4'
                )
                self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)

            self._dirty = False
            self._last_size = (w, h)
            self._last_content_sig = content_sig
            self._revision += 1

        img_fmt = ImageFormat(width=w, height=h, name=fmt_name)

        return {
            "result": TextureResult(
                texture_id=self._gpu_texture.glo,
                width=w,
                height=h,
                revision=self._revision,
                format=img_fmt,
                bbox=BBox.full_frame(w, h),
                alpha_premultiplied=False,  # Straight alpha
                color_space=ColorSpace.LINEAR,
            )
        }

    def cleanup(self) -> None:
        """Release GPU resources."""
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None
        if self._render_fbo is not None:
            self._render_fbo.release()
            self._render_fbo = None
        if self._gradient_prog is not None:
            self._gradient_prog.release()
            self._gradient_prog = None
        if self._quad_vbo is not None:
            self._quad_vbo.release()
            self._quad_vbo = None
