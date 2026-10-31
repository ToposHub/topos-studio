"""
Text Node - Rasterizes user text into a GPU texture.

Like NUKE's Text node: produces a full-frame RGBA layer with the rendered
text. The rasterization happens on the CPU via QPainter + QFont (so it
supports arbitrary fonts, weights, italics, alignment, multi-line wrapping,
letter spacing, outline and drop shadow), then the pixel buffer is uploaded
to a self-managed moderngl texture once per content change.

Color handling
--------------
The fill/stroke/shadow colors are HDR float (unclamped >= 0.0). The pixel
buffer is assembled in linear RGBA via numpy Porter-Duff over compositing so
HDR values (>1.0) survive. QPainter paints in sRGB display space, so we tag
the output color_space=SRGB and let the engine's central IDT hook
(BaseEvaluatorMixin._apply_idt_to_source_outputs) convert it to scene-linear
at the graph boundary — matching the pattern described in color_space.py.
"""

import math
import os
from typing import Any, Dict, Optional, Set, Tuple

import numpy as np
import moderngl

from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace


# ── Option enums ──
FONT_WEIGHTS: Tuple[str, ...] = ("light", "normal", "demibold", "bold", "black")
# Map our weight names to Qt numeric weight values (QFont::Weight).
_QT_WEIGHT_MAP: Dict[str, int] = {
    "light": 300,
    "normal": 400,
    "demibold": 600,
    "bold": 700,
    "black": 900,
}
ALIGNMENTS: Tuple[str, ...] = ("left", "center", "right")
VALIGNS: Tuple[str, ...] = ("top", "middle", "bottom")


def _qt_align_flags(halign: str, valign: str) -> int:
    """Return combined Qt alignment flags for the given h/v alignment names.

    Imports PyQt6 lazily so importing this node module has no Qt dependency
    at graph-deserialization time (Qt is only needed on the render/UI thread).
    """
    from PyQt6.QtCore import Qt
    h_map = {
        "left": Qt.AlignmentFlag.AlignLeft,
        "center": Qt.AlignmentFlag.AlignHCenter,
        "right": Qt.AlignmentFlag.AlignRight,
    }
    v_map = {
        "top": Qt.AlignmentFlag.AlignTop,
        "middle": Qt.AlignmentFlag.AlignVCenter,
        "bottom": Qt.AlignmentFlag.AlignBottom,
    }
    return int(h_map.get(halign, Qt.AlignmentFlag.AlignHCenter)) | \
        int(v_map.get(valign, Qt.AlignmentFlag.AlignVCenter))


class TextNode(Node):
    """Renders user text into a full-frame RGBA texture.

    CPU-rasterized with QPainter (supports font family, weight, italic,
    multi-line wrapping, alignment, letter spacing, outline and drop
    shadow), then uploaded to a self-managed GPU texture. HDR-safe RGBA
    compositing is done in numpy so values >1.0 survive into the graph.
    """

    node_type = "text"
    supports_parent_transform = True
    # Text is a valid layer root: it owns its own transform channels and
    # plugs straight into the Merge chain without an extra Transform wrapper.
    supports_layer_root = True
    node_title = "Text"
    i18n_title_key = "nodes.text.title"
    header_color = "#4B7BEC"  # Blue

    # Metadata
    SEARCH_ALIASES = [
        "text", "title", "caption", "label",
        "文字", "文本", "标题", "字幕",
        "逐字", "逐字动画", "弧形", "弧形文字",
    ]
    DESCRIPTION = "生成文字图层（字体、字号、对齐、描边、阴影）"
    TAGS = ["input", "source", "generator"]

    PARAM_DEFS = [
        # ── Dimensions (full output canvas) ──
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        # ── Content ──
        ParamDef("text", "str", "Text"),
        # ── Controlled Edit caption input ──
        # When enabled, the immutable caption snapshot in
        # CompositeRuntimeContext supplies the selected content field.
        ParamDef("caption_input_enabled", "bool", False),
        ParamDef("caption_input_field", "option", "text",
                 labels=[
                     "text", "caption_cue_id", "start", "end",
                     "language", "word_timings", "style",
                 ]),
        # ── Font ──
        ParamDef("font_family", "str", "Arial"),
        ParamDef("font_size", "int", 120, min_val=1, max_val=2048, step=1),
        ParamDef("font_weight", "option", "normal", labels=list(FONT_WEIGHTS)),
        ParamDef("italic", "bool", False),
        # ── Layout ──
        ParamDef("halign", "option", "center", labels=list(ALIGNMENTS)),
        ParamDef("valign", "option", "middle", labels=list(VALIGNS)),
        ParamDef("letter_spacing", "float", 0.0, min_val=-50.0, max_val=200.0, step=0.5),
        ParamDef("line_spacing", "float", 1.0, min_val=0.5, max_val=3.0, step=0.05),
        # ── Per-character animation (C3, Text+ / AE text-animator style).
        # Each visible character is laid out and drawn individually; its
        # position/scale/rotation/alpha follow its "birth" on the timeline.
        # The playhead frame comes from ctx.frame_number (see _do_evaluate),
        # so enabling this makes the node frame-dependent (cache keys include
        # the frame via params_require_frame_cache) without any keyframes.
        # All tuning params below are ordinary floats → keyframeable.
        ParamDef("per_char_enable", "bool", False),
        ParamDef("per_char_mode", "option", "fade",
                 labels=["fade", "slide_up", "slide_left",
                         "scale_pop", "rotate_in", "typewriter"]),
        # Frames between consecutive character births. Total animation length
        # is derived automatically: (n-1)*delay + per-char duration.
        ParamDef("per_char_delay", "float", 2.0, min_val=0.0, max_val=120.0, step=0.5),
        # 0 = each char finishes animating before the next is born (serial);
        # 1 = per-char duration is twice the delay (50% overlap). Duration is
        # delay * (1 + overlap), so total = n*delay*(1+... ) — see
        # _per_char_schedule.
        ParamDef("per_char_overlap", "float", 0.0, min_val=0.0, max_val=1.0, step=0.05),
        # Slide-in travel distance in px for slide_up / slide_left.
        ParamDef("per_char_slide_distance", "float", 40.0,
                 min_val=0.0, max_val=2048.0, step=1.0),
        # Starting rotation (deg) for rotate_in (settles to 0).
        ParamDef("per_char_rotation", "float", 45.0,
                 min_val=-360.0, max_val=360.0, step=1.0),
        # Starting scale for scale_pop (grows to 1.0).
        ParamDef("per_char_scale_from", "float", 0.5,
                 min_val=0.01, max_val=1.0, step=0.01),
        # ── Text on a path (C3 MVP: built-in arc). Glyphs run along a circle
        # of the given radius, each rotated onto the local tangent. Arc
        # angle 0 = auto (arc length = glyph advances / radius, no tracking
        # distortion); a positive angle spreads the line evenly across it.
        ParamDef("path_enable", "bool", False),
        ParamDef("path_radius", "float", 400.0, min_val=1.0, max_val=20000.0, step=1.0),
        ParamDef("path_arc_degrees", "float", 0.0, min_val=0.0, max_val=360.0, step=1.0),
        ParamDef("path_direction", "option", "up", labels=["up", "down"]),
        # ── Transform (pixel space, NUKE/Fusion convention — mirrors the
        # Transform node so the on-canvas transform handles work unchanged).
        # The handle system (viewer_handles.compute_transform_handles /
        # compute_transform_delta_params) reads exactly these keys, so the
        # text layer can be moved/scaled/rotated by dragging the gizmo.
        ParamDef("translate_x", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("translate_y", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("scale_x", "float", 1.0, min_val=0.01, max_val=20.0, step=0.01),
        ParamDef("scale_y", "float", 1.0, min_val=0.01, max_val=20.0, step=0.01),
        ParamDef("rotation", "float", 0.0, min_val=-360.0, max_val=360.0, step=0.1),
        ParamDef("pivot_mode", "option", "center", labels=list(("center", "custom"))),
        ParamDef("pivot_x", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("pivot_y", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("filter", "option", "linear", labels=list(("linear", "nearest"))),
        # ── Fill color (HDR float, unclamped) ──
        ParamDef("fill_r", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("fill_g", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("fill_b", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("fill_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # ── Outline ──
        ParamDef("outline_enable", "bool", False),
        ParamDef("outline_width", "float", 4.0, min_val=0.0, max_val=64.0, step=0.5),
        ParamDef("outline_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("outline_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("outline_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("outline_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # ── Drop shadow ──
        ParamDef("shadow_enable", "bool", False),
        ParamDef("shadow_offset_x", "float", 8.0, min_val=-512.0, max_val=512.0, step=0.5),
        ParamDef("shadow_offset_y", "float", 8.0, min_val=-512.0, max_val=512.0, step=0.5),
        ParamDef("shadow_blur", "float", 8.0, min_val=0.0, max_val=128.0, step=0.5),
        ParamDef("shadow_r", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("shadow_g", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("shadow_b", "float", 0.0, min_val=0.0, max_val=65535.0, step=0.01),
        ParamDef("shadow_a", "float", 1.0, min_val=0.0, max_val=65535.0, step=0.01),
        # ── Output metadata ──
        ParamDef("format_name", "str", "Text"),
        # NOTE: no output_color_space param. QPainter rasterizes in sRGB display
        # space; we tag color_space=SRGB so the central IDT hook converts it to
        # scene-linear for downstream compositing (mirrors how Constant is
        # documented as generating linear pixels that the Output owns the ODT for).
        # ── Hidden UI-only parameters (not serialized for rendering) ──
        ParamDef("size_preset", "str", "HD 1080", hidden=True),
        ParamDef("fill_hex", "str", "", hidden=True),
        ParamDef("outline_hex", "str", "", hidden=True),
        ParamDef("shadow_hex", "str", "", hidden=True),
    ]

    # Default canvas size presets shared with the properties panel.
    SIZE_PRESETS = (
        ("HD 1080", 1920, 1080),
        ("HD 720", 1280, 720),
        ("4K UHD", 3840, 2160),
        ("Square", 1080, 1080),
        ("Vertical", 1080, 1920),
    )

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("result", "output", "image"),
        ]
        # Provide explicit defaults so loaded graphs missing keys stay sane.
        self.parameters = {
            "width": 1920, "height": 1080,
            "text": "Text",
            "caption_input_enabled": False,
            "caption_input_field": "text",
            "font_family": "Arial",
            "font_size": 120,
            "font_weight": "normal",
            "italic": False,
            "halign": "center",
            "valign": "middle",
            "letter_spacing": 0.0,
            "line_spacing": 1.0,
            "per_char_enable": False,
            "per_char_mode": "fade",
            "per_char_delay": 2.0,
            "per_char_overlap": 0.0,
            "per_char_slide_distance": 40.0,
            "per_char_rotation": 45.0,
            "per_char_scale_from": 0.5,
            "path_enable": False,
            "path_radius": 400.0,
            "path_arc_degrees": 0.0,
            "path_direction": "up",
            "translate_x": 0.0,
            "translate_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "pivot_mode": "center",
            "pivot_x": 0.0,
            "pivot_y": 0.0,
            "filter": "linear",
            "fill_r": 1.0, "fill_g": 1.0, "fill_b": 1.0, "fill_a": 1.0,
            "outline_enable": False,
            "outline_width": 4.0,
            "outline_r": 0.0, "outline_g": 0.0, "outline_b": 0.0, "outline_a": 1.0,
            "shadow_enable": False,
            "shadow_offset_x": 8.0, "shadow_offset_y": 8.0, "shadow_blur": 8.0,
            "shadow_r": 0.0, "shadow_g": 0.0, "shadow_b": 0.0, "shadow_a": 1.0,
            "format_name": "Text",
            "size_preset": "HD 1080",
            "fill_hex": "",
            "outline_hex": "",
            "shadow_hex": "",
        }

        # Self-managed GPU texture (released in cleanup() and on content change).
        self._gpu_texture: Optional[moderngl.Texture] = None
        self._last_size: Tuple[int, int] = (0, 0)
        # Signature of every parameter that affects rendered pixels. When this
        # changes (or the texture is missing) we regenerate. This is race-free
        # — it reflects the exact values present in this evaluation rather
        # than param_revision, which is synced separately and can be stale.
        self._last_content_sig: Optional[tuple] = None
        self._revision: int = 0
        # Set True whenever the GL texture is released and recreated so the
        # evaluator can invalidate stale cache entries whose TextureResult
        # still references the now-released glo.
        self._cache_invalidated_needed: bool = False
        # C3 per-char animation: timeline frame the current raster represents.
        # Refreshed from ctx.frame_number at the top of _do_evaluate and
        # folded into _content_hash (only when per_char_enable is on), so the
        # node-internal GPU texture is re-rasterized per frame while animated.
        self._current_frame: float = 0.0
        # AUD-7413: scoped caption override.  Set for the duration of one
        # evaluation by _do_evaluate; raster helpers read the text content
        # through _effective_text() instead of the shared
        # self.parameters["text"] entry, so no evaluation temporarily
        # mutates the parameter dict (which was invisible to diagnostics,
        # serialization and any concurrent parameter reader).
        self._text_override: Optional[str] = None

    def _effective_text(self) -> str:
        """Text content for rasterization (AUD-7413).

        The caption override (controlled caption input) wins when active;
        otherwise the ``text`` parameter.  All raster/measure/hash helpers
        must use this accessor instead of reading
        ``self.parameters["text"]`` directly, so a caption evaluation never
        has to rewrite the shared parameter dict.
        """
        if self._text_override is not None:
            return str(self._text_override)
        return str(self.parameters.get("text", ""))

    # ── UI helper: per-state parameter visibility ──

    @staticmethod
    def param_visibility(params: Dict[str, object]) -> Set[str]:
        """Return the set of parameter names to HIDE for the current state.

        Used by the properties panel to show/hide rows. Always hides the
        metadata-only / hex display params (they are rendered via custom
        swatches). Outline and shadow groups hide themselves when their
        enable flag is False.
        """
        always_hidden: Set[str] = {
            "fill_hex", "outline_hex", "shadow_hex",
            "size_preset", "format_name",
        }
        hidden = set(always_hidden)
        if not bool(params.get("outline_enable", False)):
            hidden |= {
                "outline_width",
                "outline_r", "outline_g", "outline_b", "outline_a",
            }
        if not bool(params.get("shadow_enable", False)):
            hidden |= {
                "shadow_offset_x", "shadow_offset_y", "shadow_blur",
                "shadow_r", "shadow_g", "shadow_b", "shadow_a",
            }
        # pivot_x/y are only meaningful in custom pivot mode (mirrors the
        # Transform node's panel behavior).
        if str(params.get("pivot_mode", "center")) != "custom":
            hidden |= {"pivot_x", "pivot_y"}
        # C3: per-char animation and path-layout groups collapse when off.
        if not bool(params.get("per_char_enable", False)):
            hidden |= {
                "per_char_mode", "per_char_delay", "per_char_overlap",
                "per_char_slide_distance", "per_char_rotation",
                "per_char_scale_from",
            }
        if not bool(params.get("path_enable", False)):
            hidden |= {
                "path_radius", "path_arc_degrees", "path_direction",
            }
        return hidden

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, object]) -> bool:
        """Per-char animation reads the playhead (ctx.frame_number), so the
        rasterized result is frame-dependent even when nothing is keyframed.
        Path layout alone is static — keyframed path params already go through
        the keyframe_tracks signal.
        """
        return bool(params.get("per_char_enable", False))

    # ── Color helpers (mirror ConstantNode) ──

    @staticmethod
    def rgba_to_hex(r: float, g: float, b: float) -> str:
        """Convert RGB float values (0.0-1.0 range, clamped) to hex string."""
        ri = max(0, min(255, int(r * 255.0 + 0.5)))
        gi = max(0, min(255, int(g * 255.0 + 0.5)))
        bi = max(0, min(255, int(b * 255.0 + 0.5)))
        return f"#{ri:02x}{gi:02x}{bi:02x}"

    @staticmethod
    def hex_to_rgba(hex_str: str, default_alpha: float = 1.0) -> Tuple[float, float, float, float]:
        """Convert hex color string to (r, g, b, a) float tuple.

        Supports ``#rrggbb`` and ``#rrggbbaa`` formats.
        Returns ``(0.0, 0.0, 0.0, default_alpha)`` on parse failure.
        """
        hex_str = hex_str.strip().lstrip("#")
        try:
            if len(hex_str) == 8:
                r = int(hex_str[0:2], 16) / 255.0
                g = int(hex_str[2:4], 16) / 255.0
                b = int(hex_str[4:6], 16) / 255.0
                a = int(hex_str[6:8], 16) / 255.0
                return (r, g, b, a)
            elif len(hex_str) == 6:
                r = int(hex_str[0:2], 16) / 255.0
                g = int(hex_str[2:4], 16) / 255.0
                b = int(hex_str[4:6], 16) / 255.0
                return (r, g, b, default_alpha)
        except (ValueError, IndexError):
            pass
        return (0.0, 0.0, 0.0, default_alpha)

    # ── Parameter hash for cache invalidation ──

    def _content_hash(self) -> int:
        """Hash of parameters that affect the *rasterized text content*.

        Transform params (translate/scale/rotation/pivot) are EXCLUDED —
        they're applied on the GPU via _effect_type='transform' in
        _do_evaluate, so dragging the gizmo never triggers a re-raster.
        Only typography/color/effect params invalidate the cached texture.
        """
        p = self.parameters
        return hash((
            int(p.get("width", 1920)), int(p.get("height", 1080)),
            self._effective_text(),  # AUD-7413: caption override re-rasters
            str(p.get("font_family", "Arial")),
            int(p.get("font_size", 120)),
            str(p.get("font_weight", "normal")),
            bool(p.get("italic", False)),
            str(p.get("halign", "center")),
            str(p.get("valign", "middle")),
            float(p.get("letter_spacing", 0.0)),
            float(p.get("line_spacing", 1.0)),
            float(p.get("fill_r", 1.0)),
            float(p.get("fill_g", 1.0)),
            float(p.get("fill_b", 1.0)),
            float(p.get("fill_a", 1.0)),
            bool(p.get("outline_enable", False)),
            float(p.get("outline_width", 4.0)),
            float(p.get("outline_r", 0.0)),
            float(p.get("outline_g", 0.0)),
            float(p.get("outline_b", 0.0)),
            float(p.get("outline_a", 1.0)),
            bool(p.get("shadow_enable", False)),
            float(p.get("shadow_offset_x", 8.0)),
            float(p.get("shadow_offset_y", 8.0)),
            float(p.get("shadow_blur", 8.0)),
            float(p.get("shadow_r", 0.0)),
            float(p.get("shadow_g", 0.0)),
            float(p.get("shadow_b", 0.0)),
            float(p.get("shadow_a", 1.0)),
            # ── C3 per-char animation / path layout ──
            bool(p.get("per_char_enable", False)),
            str(p.get("per_char_mode", "fade")),
            float(p.get("per_char_delay", 2.0)),
            float(p.get("per_char_overlap", 0.0)),
            float(p.get("per_char_slide_distance", 40.0)),
            float(p.get("per_char_rotation", 45.0)),
            float(p.get("per_char_scale_from", 0.5)),
            bool(p.get("path_enable", False)),
            float(p.get("path_radius", 400.0)),
            float(p.get("path_arc_degrees", 0.0)),
            str(p.get("path_direction", "up")),
            # The animation playhead: part of the raster signature only while
            # per-char animation is on (path layout alone is static).
            round(float(self._current_frame), 4)
            if bool(p.get("per_char_enable", False)) else 0.0,
        ))

    # ── Rasterization (CPU, QPainter) ──────────────────────────────────────
    #
    # Each visual layer (shadow / outline / fill) is painted at device pixel
    # resolution on a transparent QImage producing an 8-bit straight-alpha
    # coverage mask. The masks are then tinted with the HDR color and
    # composited via Porter-Duff "over" in linear float, so HDR fill colors
    # (>1.0) survive and the result is straight alpha as the downstream
    # pipeline expects.

    def _rasterize_mask(self, w: int, h: int, draw_fn) -> np.ndarray:
        """Run ``draw_fn(QPainter)`` on a fresh transparent QImage and return
        a float32 (h, w, 4) RGBA coverage buffer (straight alpha, sRGB).

        ``draw_fn`` is responsible for setting up the pen/brush and drawing
        the glyphs. The mask is painted in opaque white so the alpha channel
        carries the anti-aliased glyph coverage; the HDR color is applied
        separately in _color_layer.
        """
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtCore import Qt

        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        try:
            draw_fn(p)
        finally:
            p.end()

        # QImage is ARGB32 premultiplied, byte order BGRA on little-endian.
        # Extract the alpha channel as the coverage; the RGB is white-ish so
        # we don't need it (the HDR color is applied in _color_layer).
        ptr = img.constBits()
        ptr.setsize(w * h * 4)
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(h, w, 4).copy()
        # arr channels: [B, G, R, A] (little-endian ARGB32 memory layout).
        # Use alpha as coverage; QPainter writes premultiplied, so alpha
        # already encodes the coverage regardless of the white paint.
        a = arr[:, :, 3].astype(np.float32) / 255.0
        zeros = np.zeros_like(a)
        # Return straight-alpha white coverage; RGB set to 0 since unused.
        return np.stack([zeros, zeros, zeros, a], axis=-1)

    def _build_font(self):
        """Build a QFont from the current font parameters."""
        from PyQt6.QtGui import QFont

        family = str(self.parameters.get("font_family", "Arial")).strip() or "Arial"
        size = max(1, int(self.parameters.get("font_size", 120)))
        weight_name = str(self.parameters.get("font_weight", "normal"))
        italic = bool(self.parameters.get("italic", False))
        letter_spacing = float(self.parameters.get("letter_spacing", 0.0))
        try:
            # setFamilies() lets Qt select a CJK/emoji fallback per glyph when
            # a legacy choice such as Arial does not contain the character.
            from src.features.edit.core.captions.font_resolver import FontResolver
            return FontResolver().to_qfont(
                family, size, weight_name, italic, letter_spacing
            )
        except Exception:
            # Keep the text node usable in minimal/headless environments where
            # the caption resolver cannot query Qt's font database.
            weight = _QT_WEIGHT_MAP.get(weight_name, 400)
            font = QFont(family)
            font.setPixelSize(size)
            font.setWeight(QFont.Weight(weight))
            font.setItalic(italic)
            if letter_spacing != 0.0 and size > 0:
                pct = 100.0 + (letter_spacing / size) * 100.0
                try:
                    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, float(pct))
                except Exception:
                    pass
            return font

    def _measure_text_bbox(self) -> Tuple[float, float, float, float]:
        """Measure the pixel bounding box of the laid-out text.

        Returns (x, y, w, h) in canvas-pixel space, where (x, y) is the
        top-left corner of the text block (after layout/alignment, before
        transform) and (w, h) its size. Used by the on-canvas transform
        handles so the gizmo frame hugs the actual glyphs (AE-style) rather
        than the full composite canvas.

        We build the SAME QPainterPath that _draw_text_mask paints (via the
        shared _build_text_path helper) and read its control-point rect, so
        the measurement always matches the rendered geometry — including
        multi-line text, letter spacing and font weight.

        Falls back to the full canvas if no text or Qt isn't available.
        """
        text = self._effective_text()  # AUD-7413: caption-aware read
        w = max(1, int(self.parameters.get("width", 1920)))
        h = max(1, int(self.parameters.get("height", 1080)))
        if not text.strip():
            return (0.0, 0.0, float(w), float(h))
        try:
            from PyQt6.QtWidgets import QApplication
            if QApplication.instance() is None:
                # C3: headless measure is static-layout only (no per-char /
                # arc geometry); mirrors the PIL render fallback.
                return self._measure_text_bbox_pil()
        except Exception:
            return self._measure_text_bbox_pil()

        from PyQt6.QtCore import QRectF

        font = self._build_font()
        halign = str(self.parameters.get("halign", "center"))
        valign = str(self.parameters.get("valign", "middle"))
        line_spacing = float(self.parameters.get("line_spacing", 1.0))

        # Build the glyph path in centered-origin space (same as the painter
        # transform in _draw_text_mask: text is laid out in a rect centered
        # on the origin). We pass a dummy painter device (None) since
        # _build_text_path only uses it for QTextLayout's paint device, which
        # is optional for measurement.
        class _DummyPainter:
            """Minimal stand-in so _build_text_path can call p.device()."""
            def device(self):
                return None

        rect = QRectF(-w / 2.0, -h / 2.0, float(w), float(h))
        if self._per_char_anim_enabled() or self._path_layout_enabled():
            # C3: union the per-glyph bounding rects (transformed for arc
            # placement / animation offsets). Chars are included regardless
            # of their current animation alpha so the texture size stays
            # stable while the animation plays (no per-frame texture churn,
            # no clipped late-born glyphs).
            placements = self._build_char_placements(
                _DummyPainter(), font, text, rect, halign, valign, line_spacing)
            bbox = QRectF()
            for sub_path, _alpha, _angle in placements:
                bbox = bbox.united(sub_path.boundingRect())
        else:
            path = self._build_text_path(_DummyPainter(), font, text, rect,
                                         halign, valign, line_spacing)
            bbox = path.boundingRect()
        if bbox.isEmpty() or bbox.width() <= 0 or bbox.height() <= 0:
            return (0.0, 0.0, float(w), float(h))

        # Account for stroke width — the handle frame should hug the stroked
        # glyphs, not just the fill, so the outline isn't clipped by the box.
        outline_w = 0.0
        if bool(self.parameters.get("outline_enable", False)):
            outline_w = max(0.0, float(self.parameters.get("outline_width", 0.0)))
        pad = outline_w / 2.0 + 1.0
        bbox = bbox.adjusted(-pad, -pad, pad, pad)

        # Convert from centered-origin space to canvas top-left space. The
        # rasterized texture is the base pose (text centred on the canvas,
        # identity transform), so the bbox reflects that centred position.
        # Transform params (translate/scale/rotation) are applied on the GPU
        # and don't affect the cached texture's glyph layout.
        cx = w / 2.0
        cy = h / 2.0
        out_x = cx + bbox.x()
        out_y = cy + bbox.y()
        return (out_x, out_y, float(bbox.width()), float(bbox.height()))

    def text_handle_bbox(self) -> Tuple[float, float, float, float]:
        """Public accessor for the text bounding box used by the gizmo.

        Returns (x, y, w, h) in canvas pixels (top-left origin). The
        CompositeViewer uses this as the transform "input rect" so the
        handle frame hugs the glyphs (AE-style) instead of the full canvas.
        """
        return self._measure_text_bbox()

    # ── C3: per-character animation ────────────────────────────────────────

    # Modes exposed by per_char_mode. typewriter pops characters in with no
    # transform; the others ease position/scale/rotation AND alpha.
    PER_CHAR_MODES = ("fade", "slide_up", "slide_left",
                      "scale_pop", "rotate_in", "typewriter")

    @staticmethod
    def _char_anim_state(mode: str, birth: float, duration: float,
                         frame: float, slide_dist: float,
                         rot_amount: float, scale_from: float
                         ) -> Tuple[float, float, float, float, float]:
        """Animation state of ONE character at ``frame`` — pure function.

        Returns ``(dx, dy, scale, rotation_deg, alpha)`` in the glyph's local
        frame (offsets in px, scale multiplier, rotation in degrees). The
        progress ``t`` is clamped to [0, 1] and eased with ease-out cubic
        (1-(1-t)^3); typewriter is binary (invisible before its birth frame).

        ``duration`` is the per-character animation length in frames; a
        character's ``birth`` frame is when its progress starts at 0.
        """
        if duration <= 0.0:
            duration = 1e-6
        raw = (float(frame) - float(birth)) / float(duration)
        t = max(0.0, min(1.0, raw))
        e = 1.0 - (1.0 - t) ** 3  # ease-out cubic
        if mode == "typewriter":
            return (0.0, 0.0, 1.0, 0.0, 1.0 if raw >= 0.0 else 0.0)
        if mode == "slide_up":
            return (0.0, (1.0 - e) * slide_dist, 1.0, 0.0, e)
        if mode == "slide_left":
            return ((1.0 - e) * slide_dist, 0.0, 1.0, 0.0, e)
        if mode == "scale_pop":
            s = scale_from + (1.0 - scale_from) * e
            return (0.0, 0.0, s, 0.0, e)
        if mode == "rotate_in":
            return (0.0, 0.0, 1.0, (1.0 - e) * rot_amount, e)
        # "fade" and any unknown mode degrade to a plain fade.
        return (0.0, 0.0, 1.0, 0.0, e)

    def _per_char_schedule(self) -> Tuple[float, float]:
        """Birth stagger + per-character duration (frames) from parameters.

        ``birth_i = i * delay``; ``duration = delay * (1 + overlap)`` so
        overlap 0 = strictly serial characters and overlap 1 = each
        animation lasts twice the stagger gap. Total animation length is
        derived automatically: (n-1)*delay + duration.
        """
        p = self.parameters
        delay = max(0.0, float(p.get("per_char_delay", 2.0)))
        overlap = max(0.0, min(1.0, float(p.get("per_char_overlap", 0.0))))
        duration = delay * (1.0 + overlap)
        return delay, max(duration, 1e-6)

    def _per_char_anim_enabled(self) -> bool:
        return bool(self.parameters.get("per_char_enable", False))

    def _path_layout_enabled(self) -> bool:
        return bool(self.parameters.get("path_enable", False))

    def _draw_text_mask(self, p, font, coverage_alpha: float = 1.0,
                        stroke_width: float = 0.0):
        """Draw the text coverage mask onto painter ``p``.

        Positioning/rotation/scaling is applied here; the caller has already
        translated the painter to the anchor point in canvas space. When
        ``stroke_width > 0`` the glyphs are stroked (outline mode) instead of
        filled. The mask is painted in white with the requested alpha so the
        alpha channel carries anti-aliased coverage.

        For outline mode, glyphs are converted to a QPainterPath (via
        QPainterPath.addText) and the path is stroked with the requested pen
        width — drawText() alone ignores pen width on text, so stroking the
        glyph outlines is the only way to get a real outline.
        """
        from PyQt6.QtGui import (QColor, QPen, QBrush)
        from PyQt6.QtCore import Qt, QRectF

        text = self._effective_text()  # AUD-7413: caption-aware read
        if text == "":
            return
        w = max(1, int(self.parameters.get("width", 1920)))
        h = max(1, int(self.parameters.get("height", 1080)))
        halign = str(self.parameters.get("halign", "center"))
        valign = str(self.parameters.get("valign", "middle"))
        line_spacing = float(self.parameters.get("line_spacing", 1.0))
        # Transform params (scale_x/y, rotation) are NOT applied here — they're
        # applied on the GPU via _effect_type='transform' in _do_evaluate so
        # the gizmo stays real-time. The rasterized texture is always the
        # base pose (identity transform), centered on the canvas.

        a_i = max(0, min(255, int(coverage_alpha * 255.0 + 0.5)))
        color = QColor(255, 255, 255, a_i)

        # Base pose: no rotation, no scale. The painter is already translated
        # to the canvas centre by the caller.
        p.save()

        # The text rect spans the full canvas centered on the anchor (after
        # the transform), so alignment works intuitively: with anchor at
        # canvas center and center align, text is centered.
        rect = QRectF(-w / 2.0, -h / 2.0, float(w), float(h))

        # C3: per-character animation / arc path layout paint glyph by glyph.
        # Both layers (fill and outline) iterate the SAME placement list so
        # they keep sharing exact geometry (no sub-pixel halo at the seam).
        per_char_mode = self._per_char_anim_enabled() or self._path_layout_enabled()

        if stroke_width > 0.0:
            # Outline mode: build a glyph path and stroke it. Pen width is in
            # scaled-text space (the painter is already scaled by `scale`).
            pen = QPen(color)
            pen.setWidthF(float(stroke_width))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if per_char_mode:
                for sub_path, alpha, _angle in self._build_char_placements(
                        p, font, text, rect, halign, valign, line_spacing):
                    if alpha <= 0.0:
                        continue
                    p.setOpacity(alpha)
                    p.drawPath(sub_path)
                p.setOpacity(1.0)
            else:
                path = self._build_text_path(p, font, text, rect,
                                             halign, valign, line_spacing)
                p.drawPath(path)
        else:
            # Fill mode. We still build a path and fill it (rather than
            # drawText) so fill and outline share the exact same glyph
            # geometry — otherwise the stroked outline and the filled core
            # can drift by a sub-pixel and produce a halo at the seam.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            if per_char_mode:
                for sub_path, alpha, _angle in self._build_char_placements(
                        p, font, text, rect, halign, valign, line_spacing):
                    if alpha <= 0.0:
                        continue
                    p.setOpacity(alpha)
                    p.drawPath(sub_path)
                p.setOpacity(1.0)
            else:
                path = self._build_text_path(p, font, text, rect,
                                             halign, valign, line_spacing)
                p.drawPath(path)
        p.restore()

    def _layout_visual_lines(self, p, font, text: str, rect,
                             halign: str, valign: str,
                             line_spacing: float):
        """Lay ``text`` out inside ``rect`` — shared by the whole-block and
        per-character path builders (C3).

        Splits on explicit newlines, word-wraps over-wide paragraphs against
        ``rect.width()`` and positions the block per valign. Returns
        ``(visual_lines, y0, ascent, line_height)`` where ``visual_lines`` is
        a list of ``(line_text, natural_width)`` and ``y0`` is the top of the
        first line in the painter's current (centered-origin) space.
        """
        from PyQt6.QtGui import QFontMetrics, QTextLayout, QTextOption
        from PyQt6.QtCore import Qt

        fm = QFontMetrics(font)
        ascent = fm.ascent()
        descent = fm.descent()
        line_height = max(1.0, (ascent + descent) * float(line_spacing))

        # Split on explicit newlines, then word-wrap each paragraph against
        # the rect width. Each resulting visual line becomes one row.
        paragraphs = text.split("\n")
        visual_lines = []  # list of (text, natural_width)
        max_w = float(rect.width())
        for para in paragraphs:
            if para == "":
                visual_lines.append(("", 0.0))
                continue
            # Fast path: single line that fits.
            para_w = fm.horizontalAdvance(para)
            if para_w <= max_w:
                visual_lines.append((para, float(para_w)))
                continue
            # Word-wrap this paragraph with QTextLayout.
            layout = QTextLayout(para, font, p.device() if p is not None else None)
            opt = QTextOption()
            opt.setAlignment(Qt.AlignmentFlag(_qt_align_flags(halign, valign)))
            opt.setWrapMode(QTextOption.WrapMode.WordWrap)
            opt.setTextDirection(Qt.LayoutDirection.LeftToRight)
            layout.setTextOption(opt)
            layout.beginLayout()
            while True:
                tl = layout.createLine()
                if not tl.isValid():
                    break
                tl.setLineWidth(max_w)
                ts = tl.textStart()
                tl_text = para[ts:ts + tl.textLength()]
                visual_lines.append((tl_text, float(tl.naturalTextWidth())))
            layout.endLayout()

        total_h = line_height * len(visual_lines)
        if valign == "top":
            y0 = rect.top()
        elif valign == "bottom":
            # Keep the Qt path aligned with the PIL fallback and leave the
            # same 8% bottom title-safe margin in both preview and export.
            y0 = (
                rect.bottom() - total_h
                + (line_height - (ascent + descent))
                - rect.height() * 0.08
            )
        else:  # middle
            y0 = rect.center().y() - total_h / 2.0 + \
                (line_height - (ascent + descent)) / 2.0
        return visual_lines, y0, ascent, line_height

    def _line_start_x(self, line_w: float, rect, halign: str) -> float:
        """Left edge of a laid-out line of natural width ``line_w``."""
        if halign == "right":
            return float(rect.right()) - line_w
        if halign == "center":
            return float(rect.center().x()) - line_w / 2.0
        return float(rect.left())

    def _build_text_path(self, p, font, text: str, rect,
                         halign: str, valign: str,
                         line_spacing: float):
        """Build a single QPainterPath containing every glyph of ``text``.

        The path is positioned according to halign/valign/line_spacing within
        ``rect`` (in the painter's current transformed space). Explicit ``\\n``
        newlines always start a new line; over-wide lines word-wrap to fit
        ``rect.width()``. Multi-line leading is ``line_spacing`` × the font's
        natural line height.

        Note: QTextLayout does NOT split on ``\\n`` automatically (it only
        word-wraps against lineWidth), so we split on ``\\n`` first and lay
        each paragraph out with its own QTextLayout for word-wrap.
        """
        from PyQt6.QtGui import QPainterPath
        from PyQt6.QtCore import QPointF

        visual_lines, y0, ascent, line_height = self._layout_visual_lines(
            p, font, text, rect, halign, valign, line_spacing)
        if not visual_lines:
            return QPainterPath()

        path = QPainterPath()
        for i, (line_text, line_w) in enumerate(visual_lines):
            baseline_y = y0 + i * line_height + ascent
            x = self._line_start_x(line_w, rect, halign)
            if line_text:
                path.addText(QPointF(x, baseline_y), font, line_text)
        return path

    def _build_char_placements(self, p, font, text: str, rect,
                               halign: str, valign: str,
                               line_spacing: float):
        """Build ONE QPainterPath per character (C3: per-char animation and
        arc path layout).

        Returns a list of ``(path, alpha, angle_deg)`` where each ``path`` is
        a single glyph already transformed into the painter's current space
        (centered-origin), ``alpha`` is its animation opacity in [0, 1] and
        ``angle_deg`` the glyph's TOTAL rotation (arc tangent + per-char
        entrance rotation — what the on-canvas glyph actually shows).
        Callers keep the fill and outline branches on the SAME list so both
        layers share exact geometry.

        Layout reuses the block layout (_layout_visual_lines) and walks each
        line character by character with QFontMetrics advances. Because each
        glyph is placed individually, pair kerning is not applied in this
        mode (matching AE per-character animators); letter_spacing still
        applies — it is baked into the font.

        Arc path mode (path_enable): each line's baseline is bent onto a
        circle of ``path_radius`` px centered on the line's baseline midpoint
        (below it for "up" — a rainbow — above it for "down"). ``u_arc``
        degrees distribute the line's advances evenly; 0 = auto, i.e. each
        advance maps to its own arc length (radius-driven curvature, no
        tracking distortion). Per-char animation transforms are composed in
        the glyph's local frame ON TOP of the arc placement, so the two
        features combine naturally.

        Headless note: PIL fallback does not implement per-char/arc layout;
        callers degrade to the whole-block path when Qt is unavailable.
        """
        from PyQt6.QtGui import QFontMetrics, QPainterPath, QTransform
        from PyQt6.QtCore import QPointF

        visual_lines, y0, ascent, line_height = self._layout_visual_lines(
            p, font, text, rect, halign, valign, line_spacing)
        if not visual_lines:
            return []

        fm = QFontMetrics(font)
        anim_on = self._per_char_anim_enabled()
        path_on = self._path_layout_enabled()
        anim_mode = str(self.parameters.get("per_char_mode", "fade"))
        slide_dist = max(0.0, float(self.parameters.get("per_char_slide_distance", 40.0)))
        rot_amount = float(self.parameters.get("per_char_rotation", 45.0))
        scale_from = max(0.01, min(1.0, float(self.parameters.get("per_char_scale_from", 0.5))))
        anim_delay, anim_duration = self._per_char_schedule()

        radius = max(1.0, float(self.parameters.get("path_radius", 400.0)))
        arc_deg = max(0.0, float(self.parameters.get("path_arc_degrees", 0.0)))
        arc_dir_down = str(self.parameters.get("path_direction", "up")) == "down"

        placements = []
        stagger_i = 0  # birth index — whitespace advances position but not rhythm
        for li, (line_text, line_w) in enumerate(visual_lines):
            baseline_y = y0 + li * line_height + ascent
            if not line_text:
                continue
            x_left = self._line_start_x(line_w, rect, halign)

            # Per-character advances along the (straight or arced) baseline.
            advances = [fm.horizontalAdvance(ch) for ch in line_text]
            total_adv = float(sum(advances))
            if total_adv <= 0.0:
                continue
            # Arc-length position of each glyph's centre relative to the
            # line midpoint (px, positive to the right).
            centres = []
            acc = 0.0
            for adv in advances:
                centres.append(acc + adv / 2.0 - total_adv / 2.0)
                acc += adv

            # Arc geometry for this line. The circle centre sits on the
            # baseline normal through the line midpoint; θ is measured from
            # the apex (θ=0 → the glyph sits at its straight-layout spot).
            if path_on:
                # The circle centre sits on the baseline normal through the
                # line's midpoint — which follows halign (x_left), not the
                # canvas centre: centring here made left/right-aligned text
                # jump to the middle the moment arc mode turned on.
                mid_x = x_left + total_adv / 2.0
                if arc_dir_down:
                    circ_cx, circ_cy = mid_x, baseline_y - radius
                else:
                    circ_cx, circ_cy = mid_x, baseline_y + radius

                def _theta_deg(s_rel: float) -> float:
                    if arc_deg > 0.0:
                        # Fixed spread: the whole line spans arc_deg degrees,
                        # distributed proportionally to arc-length position.
                        return (s_rel / (total_adv / 2.0)) * (arc_deg / 2.0)
                    # Auto: arc length == advance (curvature from radius).
                    return math.degrees(s_rel / radius)

            for ch, adv, s_rel in zip(line_text, advances, centres):
                w_ch = max(1.0, float(adv))
                # Glyph-local path: baseline through y=0, centred on x=0.
                glyph = QPainterPath()
                glyph.addText(QPointF(-w_ch / 2.0, 0.0), font, ch)
                if glyph.isEmpty():
                    # Whitespace/control char — still consumes stagger slot
                    # positioning above, produces no paint.
                    if not ch.isspace():
                        stagger_i += 1
                    continue

                alpha = 1.0
                dx = dy = 0.0
                scale = 1.0
                rot = 0.0
                tangent = 0.0
                if path_on:
                    theta = _theta_deg(s_rel)
                    tangent = theta if not arc_dir_down else -theta
                if anim_on:
                    birth = stagger_i * anim_delay
                    dx, dy, scale, rot, alpha = self._char_anim_state(
                        anim_mode, birth, anim_duration, self._current_frame,
                        slide_dist, rot_amount, scale_from)

                t = QTransform()
                if path_on:
                    theta_rad = math.radians(_theta_deg(s_rel))
                    if arc_dir_down:
                        gx = circ_cx + radius * math.sin(theta_rad)
                        gy = circ_cy + radius * math.cos(theta_rad)
                    else:
                        gx = circ_cx + radius * math.sin(theta_rad)
                        gy = circ_cy - radius * math.cos(theta_rad)
                    t.translate(gx, gy)
                    t.rotate(tangent)
                else:
                    t.translate(x_left + s_rel + total_adv / 2.0, baseline_y)
                t.rotate(rot)
                t.scale(scale, scale)
                t.translate(dx, dy)
                placements.append((t.map(glyph), alpha, tangent + rot))
                stagger_i += 1
        return placements

    def _draw_text_layout(self, p, font, text, rect, halign: str,
                          valign: str, line_spacing: float):
        """Draw text with custom line spacing.

        Deprecated: kept for compatibility. _build_text_path now handles
        both single-line and multi-line layout uniformly.
        """
        path = self._build_text_path(p, font, text, rect, halign, valign, line_spacing)
        from PyQt6.QtGui import QColor
        p.fillPath(path, QColor(255, 255, 255))

    # ── Compositing (numpy Porter-Duff "over") ──

    @staticmethod
    def _over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
        """Porter-Duff "over" compositing of two straight-alpha RGBA float
        buffers. Supports HDR RGB (values > 1.0) — only alpha is in [0,1].

        Result is straight alpha.
        """
        ta = top[..., 3:4]
        ba = bottom[..., 3:4]
        out_a = ta + ba * (1.0 - ta)
        safe_a = np.maximum(out_a, 1e-7)
        out_rgb = (top[..., :3] * ta + bottom[..., :3] * ba * (1.0 - ta)) / safe_a
        out = np.concatenate([out_rgb, out_a], axis=-1)
        # Where out_a is effectively zero, force RGB to zero (avoid div residue).
        zero_mask = (out_a <= 1e-7)
        if np.any(zero_mask):
            out[..., :3][zero_mask[..., 0]] = 0.0
        return out

    def _color_layer(self, mask: np.ndarray,
                     color: Tuple[float, float, float, float]) -> np.ndarray:
        """Tint a straight-alpha coverage mask with an HDR float color.

        ``mask`` is (h, w, 4) straight-alpha white coverage (QPainter output).
        ``color`` is (r, g, b, a) in [0, 65535]; r/g/b are HDR.
        The result is a straight-alpha HDR RGBA buffer.
        """
        cr, cg, cb, ca = color
        coverage = mask[..., 3:4]  # (h, w, 1)
        h, w = coverage.shape[:2]
        rgb = np.empty((h, w, 3), dtype=np.float32)
        rgb[..., 0] = cr
        rgb[..., 1] = cg
        rgb[..., 2] = cb
        out_a = coverage * ca
        return np.concatenate([rgb, out_a], axis=-1)

    # ── Controlled caption input ──

    def _update_current_frame(self, ctx) -> None:
        """Latch the timeline frame for per-char animation (C3).

        Reads ``ctx.frame_number`` (set by the evaluator from the snapshot
        playhead) and subtracts the node's time_range start when enabled.
        Falls back to 0 for direct calls without an eval context.
        """
        frame = getattr(ctx, "frame_number", 0)
        try:
            frame_f = float(frame)
        except (TypeError, ValueError):
            frame_f = 0.0
        origin = 0.0
        tr = getattr(self, "time_range", None)
        if isinstance(tr, dict) and tr.get("enabled", True):
            try:
                origin = float(tr.get("comp_start", 0) or 0)
            except (TypeError, ValueError):
                origin = 0.0
        self._current_frame = frame_f - origin

    def _resolve_caption_input_text(self, ctx) -> Optional[str]:
        """Read a controlled caption value without allowing graph writes."""
        if not bool(self.parameters.get("caption_input_enabled", False)):
            return None
        from src.features.composite.core.eval_context import CompositeEvalContext
        runtime = None
        direct_input = None
        if isinstance(ctx, CompositeEvalContext):
            runtime = ctx.runtime_context
            direct_input = ctx.caption_input
        else:
            runtime = getattr(ctx, "runtime_context", None)
            direct_input = getattr(ctx, "caption_input", None)
        value = getattr(runtime, "caption_input", None) if runtime is not None else None
        if value is None:
            value = direct_input
        if value is None:
            return None
        field = str(self.parameters.get("caption_input_field", "text"))
        if isinstance(value, dict):
            selected = value.get(field)
        else:
            selected = getattr(value, field, None)
        if selected is None:
            return None
        if isinstance(selected, (dict, list, tuple)):
            import json
            return json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str)
        return str(selected)

    # ── Evaluate ──

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate with an optional read-only caption snapshot override."""
        # C3 per-char animation: latch the playhead before any raster work.
        # Animated characters measure their progress against the node's
        # time_range start when one is set, so a title starts its animation
        # at its clip in-point rather than at composition frame 0.
        self._update_current_frame(ctx)
        caption_text = self._resolve_caption_input_text(ctx)
        if caption_text is None:
            return self._do_evaluate_with_caption(
                inputs, ctx, fb_manager, resource_pool, render_mode)

        # AUD-7413: the caption value is applied as a scoped override read
        # through _effective_text(); the shared self.parameters["text"]
        # entry is never rewritten.  The old approach temporarily mutated
        # the parameter dict — invisible to diagnostics/serialization and
        # racy against any future concurrent parameter reader; the
        # rasterizer now gets the caption through an explicit channel while
        # the source cue stays immutable and graph serialization unchanged.
        self._text_override = caption_text
        try:
            return self._do_evaluate_with_caption(
                inputs, ctx, fb_manager, resource_pool, render_mode)
        finally:
            self._text_override = None

    def _do_evaluate_with_caption(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        # Unwrap CompositeEvalContext to get the actual ModernGL context.
        from src.features.composite.core.eval_context import CompositeEvalContext
        if isinstance(ctx, CompositeEvalContext):
            ctx = ctx.gl

        canvas_w = max(1, int(self.parameters.get("width", 1920)))
        canvas_h = max(1, int(self.parameters.get("height", 1080)))
        fmt_name = self.parameters.get("format_name", "Text") or "Text"

        # The text texture is bbox-sized (just big enough for the glyphs),
        # NOT the full canvas. The on-canvas position is driven entirely by
        # the transform below (translate puts the bbox layer on the canvas).
        # This keeps the handle frame tight around the text (AE-style) AND
        # makes scale/rotate math consistent — the handle system's image
        # space IS this bbox texture.
        bbox_x, bbox_y, bbox_w, bbox_h = self._measure_text_bbox()
        tex_w = max(1, int(round(bbox_w)))
        tex_h = max(1, int(round(bbox_h)))

        # The rasterized text texture only depends on typography/color/effect
        # params (see _content_hash — transform params are excluded). So
        # dragging the on-canvas gizmo (translate/scale/rotation) NEVER
        # triggers a re-raster; only the GPU-side transform below updates.
        content_sig = self._content_hash()
        content_changed = content_sig != self._last_content_sig

        if content_changed or self._gpu_texture is None:
            if self._gpu_texture is not None:
                self._gpu_texture.release()
                self._gpu_texture = None
                self._cache_invalidated_needed = True

            data = self._render_text_pixels(tex_w, tex_h, bbox_origin=(bbox_x, bbox_y))
            # Convert from image space (row 0 = top, QImage/QPainter
            # convention) to GL texture space (row 0 = bottom, OpenGL
            # convention). See image_input_node.py:246 for the matching
            # flipud pattern.
            data = np.ascontiguousarray(np.flipud(data))
            self._gpu_texture = ctx.texture(
                (tex_w, tex_h), 4, data=data.tobytes(), dtype='f4')
            self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)

            self._dirty = False
            self._last_size = (tex_w, tex_h)
            self._last_content_sig = content_sig
            self._revision += 1

        img_fmt = ImageFormat(width=canvas_w, height=canvas_h, name=fmt_name)

        # Base (untransformed) text layer: bbox-sized texture.
        base_result = TextureResult(
            texture_id=self._gpu_texture.glo,
            width=tex_w,
            height=tex_h,
            revision=self._revision,
            format=img_fmt,
            bbox=BBox.full_frame(tex_w, tex_h),
            alpha_premultiplied=False,  # Straight alpha
            color_space=ColorSpace.SRGB,  # QPainter paints sRGB; IDT converts to linear
        )

        # ── Apply transform on the GPU (real-time, no re-raster) ──
        # The bbox texture is placed on the canvas via translate. The base
        # position is the text's laid-out position (bbox_x, bbox_y on the
        # canvas), and the user's translate_x/y offsets from there. pivot
        # defaults to the bbox centre (tex_w/2, tex_h/2) in texture space.
        #
        # The renderer's transform (_apply_transform) and compute_transform_handles
        # both use Y-down pixel space where the image's default centre lands at
        # (tex_w/2, canvas_h - tex_h/2) (lower-left area). To place the text at
        # its laid-out position (bbox_x, bbox_y) we offset:
        #   tx = bbox_x                          (X)
        #   ty = bbox_y + bbox_h - canvas_h      (Y)
        # User translate (Y-down, +ve = down) is added on top. This MUST match
        # CompositeViewer._effective_text_transform_params exactly so the
        # handle frame tracks the rendered text.
        user_tx = float(self.parameters.get("translate_x", 0.0))
        user_ty = float(self.parameters.get("translate_y", 0.0))
        sx = float(self.parameters.get("scale_x", 1.0))
        sy = float(self.parameters.get("scale_y", 1.0))
        rot = float(self.parameters.get("rotation", 0.0))

        tx = bbox_x + user_tx
        ty = (bbox_y + bbox_h - canvas_h) + user_ty

        # Resolve pivot in texture-local space (same semantics as TransformNode
        # but relative to the bbox-sized texture, not the canvas).
        if str(self.parameters.get("pivot_mode", "center")) == "custom":
            # Custom pivot is stored in canvas space; remap to texture space.
            piv_x = float(self.parameters.get("pivot_x", canvas_w / 2.0)) - bbox_x
            piv_y = float(self.parameters.get("pivot_y", canvas_h / 2.0)) - bbox_y
        else:
            piv_x = tex_w / 2.0
            piv_y = tex_h / 2.0

        # Text ALWAYS needs the GPU transform pass — even with user
        # translate/scale/rotation at their defaults (0/1/0), the bbox-sized
        # texture must still be placed at its laid-out position (bbox_x/y) on
        # the canvas. Without this pass the small texture would render at the
        # canvas origin (wrong place) or be invisible.
        filter_name = str(self.parameters.get("filter", "linear"))
        filter_idx = 0 if filter_name == "linear" else 1

        # Compute output bbox via the same matrix math as TransformNode.
        import math as _math
        rad = _math.radians(rot)
        cos_r = _math.cos(rad)
        sin_r = _math.sin(rad)
        m00 = sx * cos_r
        m01 = -sy * sin_r
        m10 = sx * sin_r
        m11 = sy * cos_r
        m02 = piv_x - sx * cos_r * piv_x + sy * sin_r * piv_y + tx
        m12 = piv_y - sx * sin_r * piv_x - sy * cos_r * piv_y + ty

        def _xform(px, py):
            return (m00 * px + m01 * py + m02, m10 * px + m11 * py + m12)
        # The output canvas is always the full composite canvas — the bbox
        # texture is placed onto it via the transform. Downstream nodes (Merge)
        # expect a canvas-sized layer, so out_w/out_h = canvas size and
        # out_bbox spans the whole canvas. (TransformNode grows the canvas to
        # hold moved content, but Text is a source whose layer always lives on
        # the composite canvas.)
        out_w = canvas_w
        out_h = canvas_h
        out_bbox = BBox(x=0.0, y=0.0, width=float(canvas_w), height=float(canvas_h))

        return {
            "result": TextureResult(
                texture_id=-1,
                width=canvas_w,
                height=canvas_h,
            ),
            "_effect_type": "transform",
            "_effect_input": base_result,
            "_effect_params": {
                "translate": [tx, ty],
                "scale": [sx, sy],
                "rotation": rot,
                "pivot": [piv_x, piv_y],
                "filter": filter_idx,
                "out_w": out_w,
                "out_h": out_h,
                "out_bbox": out_bbox,
            },
        }

    def _render_text_pixels(self, w: int, h: int,
                            bbox_origin: Optional[Tuple[float, float]] = None) -> np.ndarray:
        """Build a (h, w, 4) float32 RGBA buffer sized to the text bbox.

        The output texture is just big enough to hold the glyphs (plus
        outline/shadow padding) — NOT the full canvas. Text is laid out from
        the top-left corner of this bbox-sized buffer, so the texture can be
        placed on the composite canvas via a transform whose translate puts
        the bbox's top-left at the right canvas position.

        This keeps the on-canvas handle frame tight around the text (AE-style)
        AND makes scale/rotate math consistent, because the handle system's
        image-space IS this bbox texture — no coordinate-system mismatch.

        AUD-7411 fix: ``bbox_origin`` is the (x, y) of the text bbox on the
        canvas, already measured by the caller (``_do_evaluate_with_caption``
        calls :meth:`_measure_text_bbox` to size this very texture). Passing
        it in avoids re-running the full QPainterPath/QTextLayout glyph
        measurement here — with per-character animation every frame
        re-rasterizes, and the duplicate layout doubled the dominant CPU
        cost. ``None`` falls back to measuring (legacy direct callers).

        Composites shadow → outline → fill into a single straight-alpha HDR
        buffer. Transparent outside the glyph area.

        Falls back to a transparent buffer when no QApplication exists.
        """
        # Guard: QPainter on QImage / QFontDatabase both require a living
        # QApplication. Without one (headless / AI automation), fall back to
        # a PIL-based renderer so subtitles still burn into exported frames.
        # C3: the PIL path renders the static whole-block layout only —
        # per-char animation and arc layout silently degrade headless.
        try:
            from PyQt6.QtWidgets import QApplication
            if QApplication.instance() is None:
                return self._render_text_pixels_pil(w, h)
        except Exception:
            return self._render_text_pixels_pil(w, h)

        from PyQt6.QtCore import QPointF

        text = self._effective_text()  # AUD-7413: caption-aware read
        font = self._build_font()

        # Empty canvas (transparent) for empty text.
        canvas = np.zeros((h, w, 4), dtype=np.float32)

        if not text.strip():
            return canvas

        # ``text_handle_bbox()`` measures the glyphs in the original canvas
        # coordinate system, while the returned buffer is a compact bbox-sized
        # texture. Keep the original layout dimensions while rasterizing so a
        # long Chinese line is not re-wrapped against the compact texture
        # width. Then translate the already-laid-out glyphs by the bbox origin
        # into the compact buffer. Re-wrapping here would make the measured
        # single-line height too small and clip every line except the last one.
        layout_width = max(1, int(self.parameters.get("width", 1920)))
        layout_height = max(1, int(self.parameters.get("height", 1080)))
        if bbox_origin is not None:
            # AUD-7411: reuse the caller's measurement (same layout state —
            # the caller sized this texture from it one line earlier).
            bbox_x, bbox_y = float(bbox_origin[0]), float(bbox_origin[1])
        else:
            bbox_x, bbox_y, _bbox_w, _bbox_h = self.text_handle_bbox()
        cx = layout_width / 2.0 - bbox_x
        cy = layout_height / 2.0 - bbox_y

        def _paint_layer(draw_fn) -> np.ndarray:
            return self._rasterize_mask(w, h, draw_fn)

        # ── Fill mask ──
        fill = (
            float(self.parameters.get("fill_r", 1.0)),
            float(self.parameters.get("fill_g", 1.0)),
            float(self.parameters.get("fill_b", 1.0)),
            float(self.parameters.get("fill_a", 1.0)),
        )

        def _draw_fill(p):
            p.translate(QPointF(cx, cy))
            self._draw_text_mask(p, font, coverage_alpha=fill[3])
        fill_mask = _paint_layer(_draw_fill)

        # ── Outline mask (stroke of the same glyphs) ──
        outline_mask: Optional[np.ndarray] = None
        outline_color = (0.0, 0.0, 0.0, 1.0)
        if bool(self.parameters.get("outline_enable", False)):
            outline_width = max(0.0, float(self.parameters.get("outline_width", 4.0)))
            if outline_width > 0.0:
                outline_color = (
                    float(self.parameters.get("outline_r", 0.0)),
                    float(self.parameters.get("outline_g", 0.0)),
                    float(self.parameters.get("outline_b", 0.0)),
                    float(self.parameters.get("outline_a", 1.0)),
                )

                def _draw_outline(p):
                    p.translate(QPointF(cx, cy))
                    self._draw_text_mask(p, font,
                                         coverage_alpha=outline_color[3],
                                         stroke_width=outline_width)
                outline_mask = _paint_layer(_draw_outline)

        # ── Shadow mask (offset + blur of the fill glyphs) ──
        shadow_mask: Optional[np.ndarray] = None
        shadow_color = (0.0, 0.0, 0.0, 1.0)
        if bool(self.parameters.get("shadow_enable", False)):
            shadow_color = (
                float(self.parameters.get("shadow_r", 0.0)),
                float(self.parameters.get("shadow_g", 0.0)),
                float(self.parameters.get("shadow_b", 0.0)),
                float(self.parameters.get("shadow_a", 1.0)),
            )
            sx = float(self.parameters.get("shadow_offset_x", 8.0))
            sy = float(self.parameters.get("shadow_offset_y", 8.0))

            def _draw_shadow(p):
                p.translate(QPointF(cx + sx, cy + sy))
                self._draw_text_mask(p, font, coverage_alpha=shadow_color[3])
            shadow_mask = _paint_layer(_draw_shadow)

            blur = max(0.0, float(self.parameters.get("shadow_blur", 8.0)))
            if blur > 0.0 and shadow_mask is not None:
                shadow_mask = self._gaussian_blur(shadow_mask, blur)

        # ── Composite: shadow → outline → fill ──
        if shadow_mask is not None:
            shadow_layer = self._color_layer(shadow_mask, shadow_color)
            canvas = self._over(shadow_layer, canvas)
        if outline_mask is not None:
            outline_layer = self._color_layer(outline_mask, outline_color)
            canvas = self._over(outline_layer, canvas)
        fill_layer = self._color_layer(fill_mask, fill)
        canvas = self._over(fill_layer, canvas)

        # Safety: drop any NaN/inf from compositing residue.
        np.nan_to_num(canvas, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        # Clamp alpha to [0, 1] for valid texture data (RGB stays HDR).
        canvas[..., 3] = np.clip(canvas[..., 3], 0.0, 1.0)
        return canvas

    # ── PIL fallback (headless / AI automation without QApplication) ──── #

    _PIL_FONT_SEARCH_PATHS = (
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
    )
    _pil_font_cache: Dict[Tuple[str, int, bool], Any] = {}
    _pil_font_files: list[str] = []

    def _load_pil_font(self) -> Any:
        """Load a PIL ImageFont matching the current font parameters.

        Falls back through: requested family → common CJK fonts → DejaVu →
        PIL default. Caches by (family, size, bold) so repeated frames are fast.
        """
        from PIL import ImageFont

        family = str(self.parameters.get("font_family", "Arial")).strip() or "Arial"
        size = max(1, int(self.parameters.get("font_size", 120)))
        weight_name = str(self.parameters.get("font_weight", "normal"))
        bold = weight_name in ("bold", "black", "demibold")

        cache_key = (family.lower(), size, bold)
        if cache_key in self._pil_font_cache:
            return self._pil_font_cache[cache_key]


        # Normalize family name for file matching (remove spaces, lowercase).
        fam_norm = family.lower().replace(" ", "")
        # CJK + universal fallback chain (covers Chinese/Japanese/Korean/Latin).
        fallback_keywords = (
            "stheiti", "pingfang", "hiragino", "notosanscjk", "notosans",
            "wenquanyi", "simsun", "simhei", "msyh",
            "arial", "helvetica", "dejavusans", "liberationsans",
        )

        # Scan font directories for candidate files.
        candidates: list[str] = []
        for font_dir in self._PIL_FONT_SEARCH_PATHS:
            if not os.path.isdir(font_dir):
                continue
            # Walk the directory tree (handles nested font folders).
            for root, _dirs, files in os.walk(font_dir):
                for fname in files:
                    lower = fname.lower()
                    if not lower.endswith((".ttf", ".ttc", ".otf")):
                        continue
                    # Priority 1: matches requested family name
                    # Priority 2: matches a fallback keyword
                    if fam_norm and fam_norm in lower:
                        candidates.insert(0, os.path.join(root, fname))  # high priority
                    elif any(kw in lower for kw in fallback_keywords):
                        candidates.append(os.path.join(root, fname))

        # De-duplicate while preserving order
        seen: set[str] = set()
        unique = [c for c in candidates if not (c in seen or seen.add(c))]

        font = None
        for path in unique:
            try:
                font = ImageFont.truetype(path, size)
                self._pil_font_files = unique  # cache for supersampled reloads
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        self._pil_font_cache[cache_key] = font
        return font

    def _render_text_pixels_pil(self, w: int, h: int) -> np.ndarray:
        """PIL-based text rasterization fallback (no QApplication required).

        Produces the same (h, w, 4) float32 straight-alpha RGBA buffer as the
        Qt path, compositing shadow → outline → fill. Used for headless export
        (AI automation, CLI scripts) where Qt's QFontDatabase is unavailable.

        ``w`` / ``h`` are the texture (bbox) dimensions from
        :meth:`_measure_text_bbox_pil` — text fills this buffer edge-to-edge.
        """
        from PIL import Image, ImageDraw, ImageFilter

        text = self._effective_text()  # AUD-7413: caption-aware read
        canvas = np.zeros((h, w, 4), dtype=np.float32)
        if not text.strip():
            return canvas

        # Load font at the CURRENT size (from parameters, not the passed w/h).
        font = self._load_pil_font()

        # 2x supersampling for smoother edges.
        ss = 2
        fw, fh = w * ss, h * ss

        # Load font at supersampled size for crisp rendering.
        family = str(self.parameters.get("font_family", "Arial")).strip() or "Arial"
        base_size = max(1, int(self.parameters.get("font_size", 120)))
        ss_size = base_size * ss
        weight_name = str(self.parameters.get("font_weight", "normal"))
        bold = weight_name in ("bold", "black", "demibold")
        ss_cache_key = (family.lower(), ss_size, bold)
        font_ss = self._pil_font_cache.get(ss_cache_key)
        if font_ss is None:
            from PIL import ImageFont
            # Find the same font file at supersampled size.
            # Re-scan with the larger size.
            for path in self._pil_font_files:
                try:
                    font_ss = ImageFont.truetype(path, ss_size)
                    break
                except Exception:
                    continue
            if font_ss is None:
                font_ss = font  # fallback to base size
            self._pil_font_cache[ss_cache_key] = font_ss

        # Create transparent canvas at supersampled resolution.
        img = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))

        # Measure text to center it in the texture.
        measure_img = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        measure_draw = ImageDraw.Draw(measure_img)
        bbox = measure_draw.textbbox((0, 0), text, font=font_ss)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (fw - tw) / 2.0 - bbox[0]
        ty = (fh - th) / 2.0 - bbox[1]

        # Colors (PIL uses 0-255).
        # AUD-7412 fix: the fill/outline/shadow parameters are HDR-domain
        # floats (max_val=65535). The Qt path composites them as float32
        # directly; this PIL fallback packs into 8-bit RGBA, where values
        # above 1.0 must be folded into the 0-255 byte domain instead of
        # overflowing (int(4.0*255)=1020 bleeds across channels in Pillow's
        # ink packing). Clamp to [0, 255] — the 8-bit ceiling of this
        # display-domain approximation (documented limitation of the
        # headless fallback, which cannot carry HDR colour anyway).
        def _color_8bit(comp: str) -> int:
            return max(0, min(255, int(float(self.parameters.get(comp, 1.0)) * 255)))

        fill_rgba = (
            _color_8bit("fill_r"), _color_8bit("fill_g"),
            _color_8bit("fill_b"), _color_8bit("fill_a"),
        )
        outline_enable = bool(self.parameters.get("outline_enable", False))
        outline_width = max(0.0, float(self.parameters.get("outline_width", 4.0)))
        outline_rgba = (
            _color_8bit("outline_r"), _color_8bit("outline_g"),
            _color_8bit("outline_b"), _color_8bit("outline_a"),
        )
        shadow_enable = bool(self.parameters.get("shadow_enable", False))
        shadow_rgba = (
            _color_8bit("shadow_r"), _color_8bit("shadow_g"),
            _color_8bit("shadow_b"), _color_8bit("shadow_a"),
        )
        sx = float(self.parameters.get("shadow_offset_x", 8.0)) * ss
        sy = float(self.parameters.get("shadow_offset_y", 8.0)) * ss
        sblur = max(0.0, float(self.parameters.get("shadow_blur", 8.0))) * ss

        # Draw layers and composite: shadow → outline → fill.
        def _layer(x, y, fill, stroke_w=0, stroke_fill=None):
            lyr = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
            d = ImageDraw.Draw(lyr)
            kw = {"fill": fill}
            if stroke_w > 0 and stroke_fill is not None:
                kw["stroke_width"] = max(1, int(stroke_w * ss))
                kw["stroke_fill"] = stroke_fill
            d.text((x, y), text, font=font_ss, **kw)
            return lyr

        if shadow_enable:
            sl = _layer(tx + sx, ty + sy, shadow_rgba)
            if sblur > 0:
                sl = sl.filter(ImageFilter.GaussianBlur(radius=sblur / 2))
            img = Image.alpha_composite(img, sl)
        if outline_enable and outline_width > 0:
            ol = _layer(tx, ty, fill_rgba, stroke_w=outline_width, stroke_fill=outline_rgba)
            img = Image.alpha_composite(img, ol)
        else:
            fl = _layer(tx, ty, fill_rgba)
            img = Image.alpha_composite(img, fl)

        # Downsample to target resolution (LANCZOS = high quality).
        img = img.resize((w, h), Image.LANCZOS)

        # PIL RGBA → numpy float32 straight-alpha.
        arr = np.asarray(img, dtype=np.float32) / 255.0
        np.nan_to_num(arr, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        arr[..., 3] = np.clip(arr[..., 3], 0.0, 1.0)
        return arr

    def _measure_text_bbox_pil(self) -> Tuple[float, float, float, float]:
        """PIL-based text bbox measurement for headless mode.

        Returns (x, y, w, h) in canvas-pixel space. valign controls the y
        position: top → canvas top, middle → canvas center, bottom → canvas
        bottom (with an 8% bottom safe-area margin).
        """
        from PIL import Image, ImageDraw

        text = self._effective_text()  # AUD-7413: caption-aware read
        w = max(1, int(self.parameters.get("width", 1920)))
        h = max(1, int(self.parameters.get("height", 1080)))
        if not text.strip():
            return (0.0, 0.0, float(w), float(h))

        # 自适应字号（与 _clip_to_text_params 一致）
        fs = int(self.parameters.get("font_size", 0))
        if fs <= 0:
            self.parameters["font_size"] = max(12, int(h * 0.055))

        font = self._load_pil_font()
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), text, font=font)

        outline_w = 0.0
        if bool(self.parameters.get("outline_enable", False)):
            outline_w = max(0.0, float(self.parameters.get("outline_width", 0.0)))
        pad = outline_w / 2.0 + 1.0

        text_w = float(bbox[2] - bbox[0] + 2 * pad)
        text_h = float(bbox[3] - bbox[1] + 2 * pad)

        # valign → canvas y 位置
        valign = str(self.parameters.get("valign", "middle")).lower()
        if valign == "top":
            out_y = pad
        elif valign == "bottom":
            # 底部安全区：距画面底部 8% 高度
            out_y = float(h) - text_h - float(h) * 0.08
        else:  # middle
            out_y = (float(h) - text_h) / 2.0

        # halign → canvas x 位置
        halign = str(self.parameters.get("halign", "center")).lower()
        if halign == "left":
            out_x = pad
        elif halign == "right":
            out_x = float(w) - text_w - pad
        else:  # center
            out_x = (float(w) - text_w) / 2.0

        return (out_x, out_y, text_w, text_h)

    def _gaussian_blur(self, rgba: np.ndarray, radius: float) -> np.ndarray:
        """Approximate Gaussian blur on a straight-alpha RGBA float buffer.

        Uses a small separable kernel sized to ``radius``. Pure-numpy so it
        works without scipy; for large radii the cost is bounded by keeping
        the kernel modest.
        """
        radius = float(radius)
        if radius <= 0.0:
            return rgba
        # Build a 1D gaussian kernel.
        sigma = max(radius / 3.0, 0.5)
        r = int(math.ceil(sigma * 3.0))
        r = min(r, 64)  # cap kernel size
        xs = np.arange(-r, r + 1, dtype=np.float32)
        kernel = np.exp(-(xs * xs) / (2.0 * sigma * sigma))
        kernel /= kernel.sum()
        return self._separable_convolve(rgba, kernel)

    @staticmethod
    def _separable_convolve(rgba: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        """Apply a 1D kernel horizontally then vertically to each channel.

        Uses np.pad with edge mode so the blur doesn't darken at the canvas
        boundary (matches the typical drop-shadow look).
        """
        r = (kernel.shape[0] - 1) // 2
        k = kernel.astype(np.float32)

        def _conv_axis(buf: np.ndarray, axis: int) -> np.ndarray:
            pad = [(0, 0), (0, 0), (0, 0)]
            pad[axis] = (r, r)
            padded = np.pad(buf, pad, mode='edge')
            out = np.zeros_like(buf)
            for i, wv in enumerate(k):
                sl = [slice(None), slice(None), slice(None)]
                sl[axis] = slice(i, i + buf.shape[axis])
                out += wv * padded[tuple(sl)]
            return out

        tmp = _conv_axis(rgba, 0)  # vertical
        out = _conv_axis(tmp, 1)   # horizontal
        return out

    def cleanup(self) -> None:
        """Release GPU resources."""
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None
