"""
Viewer Handles - Control handle definitions and drawing for CompositeViewer.

Provides handle types, position calculations, and drawing primitives for
Transform, CornerPin, and other node control handles in the viewer overlay.

This module implements S5-09 from the composite node upgrade plan:
- Transform handles: pivot anchor, scale-X-L/R, scale-Y-T/B, internal rotate
- CornerPin handles: corner 1-4, source corners (semi-transparent)
- Cosmetic pen effect: handle size stays constant regardless of zoom
"""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from PyQt6.QtCore import QPointF

# ── Handle Color Definitions ────────────────────────────────────────────────

# Standard handle colors (RGBA, normalized 0-1)
HANDLE_COLOR_NORMAL = (1.0, 1.0, 1.0, 0.9)      # White for normal handles
HANDLE_COLOR_HOVER = (1.0, 0.9, 0.3, 1.0)       # Yellow for hover state
HANDLE_COLOR_ACTIVE = (1.0, 0.5, 0.0, 1.0)      # Orange for active/dragging
HANDLE_COLOR_CENTER = (1.0, 1.0, 1.0, 0.8)      # Center/pivot handle
HANDLE_COLOR_ROTATE = (0.0, 1.0, 1.0, 0.9)      # Cyan for rotate handle
HANDLE_COLOR_SCALE_X = (0.5, 1.0, 0.5, 0.9)     # Light green for scale-X
HANDLE_COLOR_SCALE_Y = (0.5, 0.5, 1.0, 0.9)     # Light blue for scale-Y
HANDLE_COLOR_CORNERPIN = (1.0, 0.8, 0.2, 0.9)   # Gold for cornerpin
HANDLE_COLOR_SOURCE = (0.6, 0.6, 0.6, 0.4)      # Semi-transparent gray for source corners
HANDLE_COLOR_BBOX = (1.0, 0.3, 0.3, 0.5)        # Red-tint for bbox outline

# Transform rotate handle placement: the handle sits directly above the pivot
# along the image's vertical axis, offset (1 - INSET) of the half-height.
# When the pivot is centered this is the same spot as the old "on the pivot
# guide, inside the frame" placement; off-center it keeps the pivot→handle line
# parallel to the frame's vertical edges instead of pointing at the top-middle.
# INSET 0.30 → 0.53：旋转手柄触达 (1-INSET) 从 0.70 缩到 ≈0.47（缩短 1/3）。
TRANSFORM_ROTATE_HANDLE_INSET = 0.53

# DaVinci-style colors for Mask/Roto handles (S5-11)
# Border: dark gray (60/255 ≈ 0.235), Fill: white, Feather/Rotate: cyan
MASK_COLOR_BORDER = (0.235, 0.235, 0.235, 1.0)       # Dark gray border (DaVinci style)
MASK_COLOR_FILL = (1.0, 1.0, 1.0, 0.94)              # White fill with slight transparency
MASK_COLOR_CENTER = (1.0, 1.0, 1.0, 0.7)             # Center crosshair (slightly transparent)
MASK_COLOR_FEATHER = (0.0, 1.0, 1.0, 0.86)           # Cyan for feather handles (RGB: 0,255,255 → 0,1,1)
MASK_COLOR_ROTATE = (0.0, 1.0, 1.0, 0.86)            # Cyan for rotate handle (same as feather)
MASK_COLOR_CORNER = (1.0, 1.0, 1.0, 0.94)            # White for corner handles
MASK_COLOR_WIDTH = (1.0, 1.0, 1.0, 0.94)             # White for width handles
MASK_COLOR_HEIGHT = (1.0, 1.0, 1.0, 0.94)            # White for height handles
MASK_COLOR_TANGENT = (0.7, 0.7, 0.7, 0.78)           # Gray for Bezier tangent handles
MASK_COLOR_OPACITY = (1.0, 1.0, 0.5, 0.86)           # Yellow tint for opacity control

# Handle border color (for drawing outline)
MASK_BORDER_COLOR = (60/255.0, 60/255.0, 60/255.0, 200/255.0)  # DaVinci: dark gray border


# ── Transform Handle Types ──────────────────────────────────────────────────

class TransformHandleType(Enum):
    """Handle types for Transform node interaction.

    Corresponds to S5-B control handle specifications:
    - CENTER: Legacy/virtual move handle (image-body drag translates)
    - PIVOT: Rotation/scale anchor point
    - SCALE_X_L: Left edge scale handle (horizontal)
    - SCALE_X_R: Right edge scale handle (horizontal)
    - SCALE_Y_T: Top edge scale handle (vertical)
    - SCALE_Y_B: Bottom edge scale handle (vertical)
    - SCALE_CORNER_TL: Top-left corner (dual axis)
    - SCALE_CORNER_TR: Top-right corner (dual axis)
    - SCALE_CORNER_BL: Bottom-left corner (dual axis)
    - SCALE_CORNER_BR: Bottom-right corner (dual axis)
    - ROTATE: Rotation handle (cyan, inside the top-edge → pivot guide)
    """
    CENTER = "center"
    PIVOT = "pivot"
    SCALE_X_L = "scale_x_left"
    SCALE_X_R = "scale_x_right"
    SCALE_Y_T = "scale_y_top"
    SCALE_Y_B = "scale_y_bottom"
    SCALE_CORNER_TL = "scale_corner_tl"
    SCALE_CORNER_TR = "scale_corner_tr"
    SCALE_CORNER_BL = "scale_corner_bl"
    SCALE_CORNER_BR = "scale_corner_br"
    ROTATE = "rotate"


class CornerPinHandleType(Enum):
    """Handle types for CornerPin node interaction.

    - TO_1: Destination corner 1 (top-left)
    - TO_2: Destination corner 2 (top-right)
    - TO_3: Destination corner 3 (bottom-right)
    - TO_4: Destination corner 4 (bottom-left)
    - FROM_1: Source corner 1 (reference, semi-transparent)
    - FROM_2: Source corner 2 (reference)
    - FROM_3: Source corner 3 (reference)
    - FROM_4: Source corner 4 (reference)
    """
    TO_1 = "to1"
    TO_2 = "to2"
    TO_3 = "to3"
    TO_4 = "to4"
    FROM_1 = "from1"
    FROM_2 = "from2"
    FROM_3 = "from3"
    FROM_4 = "from4"


class MaskHandleType(Enum):
    """Handle types for Mask/RotoShape node interaction.

    Implements S5-11 control point specifications.

    Mask handles are based on geometric shape parameters:
    - CENTER: Center point (crosshair, draggable for translate)
    - ROTATE: Rotation handle (cyan, fixed radius outside bbox)
    - FEATHER_T/B/L/R: Feather control points on edges (cyan)
    - WIDTH_L/R: Width control points (horizontal edges)
    - HEIGHT_T/B: Height control points (vertical edges)
    - CORNER_TL/TR/BL/BR: Corner handles (for resizing both axes)

    For polygon shapes:
    - POLYGON_RADIUS: Radius control point (center to vertex)

    Note: MaskNode and RotoShapeNode share similar parameters,
    so this enum covers both node types.
    """
    CENTER = "mask_center"
    ROTATE = "mask_rotate"
    FEATHER_T = "mask_feather_t"       # Top edge feather
    FEATHER_B = "mask_feather_b"       # Bottom edge feather
    FEATHER_L = "mask_feather_l"       # Left edge feather
    FEATHER_R = "mask_feather_r"       # Right edge feather
    WIDTH_L = "mask_width_l"           # Left edge (width control)
    WIDTH_R = "mask_width_r"           # Right edge (width control)
    HEIGHT_T = "mask_height_t"         # Top edge (height control)
    HEIGHT_B = "mask_height_b"         # Bottom edge (height control)
    CORNER_TL = "mask_corner_tl"       # Top-left corner
    CORNER_TR = "mask_corner_tr"       # Top-right corner
    CORNER_BL = "mask_corner_bl"       # Bottom-left corner
    CORNER_BR = "mask_corner_br"       # Bottom-right corner
    POLYGON_RADIUS = "mask_polygon_radius"  # Polygon radius control


class RotoHandleType(Enum):
    """Handle types for RotoShape node interaction (S5-11).

    Roto handles are similar to Mask handles but with additional controls:
    - CENTER: Center point for shape positioning
    - ROTATE: Rotation handle (cyan)
    - FEATHER_T/B/L/R: Feather control points
    - WIDTH_L/R, HEIGHT_T/B: Size control points
    - CORNER_TL/TR/BL/BR: Corner handles
    - POLYGON_RADIUS: Radius control for polygons
    - OPACITY: Opacity control point (optional, separate from shape)

    Future extension: Bezier curve control points for custom shapes.
    """
    CENTER = "roto_center"
    ROTATE = "roto_rotate"
    FEATHER_T = "roto_feather_t"
    FEATHER_B = "roto_feather_b"
    FEATHER_L = "roto_feather_l"
    FEATHER_R = "roto_feather_r"
    WIDTH_L = "roto_width_l"
    WIDTH_R = "roto_width_r"
    HEIGHT_T = "roto_height_t"
    HEIGHT_B = "roto_height_b"
    CORNER_TL = "roto_corner_tl"
    CORNER_TR = "roto_corner_tr"
    CORNER_BL = "roto_corner_bl"
    CORNER_BR = "roto_corner_br"
    POLYGON_RADIUS = "roto_polygon_radius"
    OPACITY = "roto_opacity"           # Opacity slider control (optional)


# ── Handle Data Structures ──────────────────────────────────────────────────

@dataclass
class HandleDefinition:
    """Definition of a viewer control handle.

    Attributes:
        handle_type: Enum value identifying the handle type
        ndc_position: Position in OpenGL NDC coordinates (-1 to 1)
        color: RGBA tuple (normalized 0-1)
        radius: Screen-pixel radius for hit testing and drawing
        is_source: True if this is a source/reference handle (drawn semi-transparent)
        cursor_hint: Suggested cursor shape when hovering this handle
    """
    handle_type: Enum
    ndc_position: Tuple[float, float]
    color: Tuple[float, float, float, float]
    radius: float = 8.0  # Screen-pixel radius
    is_source: bool = False
    cursor_hint: str = "size_all"  # Qt cursor shape name


@dataclass
class TransformHandlesData:
    """Computed handle positions for a Transform node.

    All positions are in NDC coordinates relative to the current viewport.
    """
    center: Tuple[float, float]  # Image center (for translate)
    pivot: Tuple[float, float]   # Rotation pivot
    scale_x_left: Tuple[float, float]
    scale_x_right: Tuple[float, float]
    scale_y_top: Tuple[float, float]
    scale_y_bottom: Tuple[float, float]
    scale_corner_tl: Tuple[float, float]
    scale_corner_tr: Tuple[float, float]
    scale_corner_bl: Tuple[float, float]
    scale_corner_br: Tuple[float, float]
    rotate: Tuple[float, float]  # Rotate handle position on top-edge -> pivot guide
    bbox_rect: Tuple[float, float, float, float]  # NDC bounds (x0, y0, x1, y1)
    image_size: Tuple[int, int]  # Original image dimensions
    # Ordered transformed corners (TL, TR, BR, BL) in NDC — the rotated frame.
    # Drawn as a line-loop outline (Fusion-style). AABB bbox_rect is kept only
    # for compatibility / hit extension.
    frame_corners: Tuple[Tuple[float, float], Tuple[float, float],
                         Tuple[float, float], Tuple[float, float]] = ((0.0, 0.0),) * 4


# ── Geometry Helpers ────────────────────────────────────────────────────────


def _wrap_180(angle: float) -> float:
    """Wrap an angle in degrees into the (-180, 180] range."""
    while angle > 180.0:
        angle -= 360.0
    while angle <= -180.0:
        angle += 360.0
    return angle


_SHIFT_MODIFIER_MASK = 0x2000000  # Qt.KeyboardModifier.ShiftModifier


def _normalize_modifiers(modifiers: Any) -> int:
    """Return an int modifier bitmask from an int OR a PyQt6 modifier enum.

    PyQt6's strict enums reject mixed ``enum & int`` bitwise arithmetic
    (``Qt.KeyboardModifier.ShiftModifier & 0x2000000`` raises TypeError),
    which crashed transform corner drags before the defensive cast existed
    (see the comment in ``compute_transform_delta_params``). Callers pass
    either ``event.modifiers()`` (a Qt flag object) or a plain int bitmask;
    both must work (AUD-11306 — mask/roto delta functions need the same
    defense as the transform one).
    """
    if isinstance(modifiers, int):
        return modifiers
    return int(getattr(modifiers, "value", 0))


def _handle_local_anchor(
    handle_id: str,
    image_width: float,
    image_height: float,
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Pre-transform image-pixel position of a scale handle and its opposite anchor.

    The anchor is the corner/edge-midpoint that stays fixed in world space while
    the handle is dragged (Fusion default). Used by ``compute_transform_delta_params``.

    Returns:
        (handle_local, anchor_local) in image pixels (y-down, top-left origin).
    """
    w = image_width
    h = image_height
    hw = w / 2.0
    hh = h / 2.0
    table = {
        # corner handles: (handle, opposite corner)
        "scale_corner_tl": ((0.0, 0.0), (w, h)),
        "scale_corner_tr": ((w, 0.0), (0.0, h)),
        "scale_corner_br": ((w, h), (0.0, 0.0)),
        "scale_corner_bl": ((0.0, h), (w, 0.0)),
        # edge handles: (handle, opposite edge midpoint)
        "scale_x_left":   ((0.0, hh), (w, hh)),
        "scale_x_right":  ((w, hh), (0.0, hh)),
        "scale_y_top":    ((hw, 0.0), (hw, h)),
        "scale_y_bottom": ((hw, h), (hw, 0.0)),
    }
    return table.get(handle_id, ((hw, hh), (hw, hh)))


@dataclass
class CornerPinHandlesData:
    """Computed handle positions for a CornerPin node.

    Positions are normalized (0-1) and then converted to NDC for display.
    """
    to_corners: List[Tuple[float, float]]  # Destination corners (NDC)
    from_corners: List[Tuple[float, float]]  # Source corners (NDC, reference)
    image_size: Tuple[int, int]


# ── Handle Position Calculators ─────────────────────────────────────────────

def compute_transform_handles(
    translate_x: float,
    translate_y: float,
    scale_x: float,
    scale_y: float,
    rotation: float,
    pivot_mode: str,
    pivot_x: float,
    pivot_y: float,
    image_width: int,
    image_height: int,
    image_ndc_rect: Tuple[float, float, float, float],
    canvas_width: int = 0,
    canvas_height: int = 0,
) -> TransformHandlesData:
    """Compute all handle positions for a Transform node.

    Transform order: translate -> rotate (around pivot) -> scale
    The handles are positioned in the transformed output space.

    Args:
        translate_x, translate_y: Pixel translation values
        scale_x, scale_y: Scale factors
        rotation: Rotation in degrees
        pivot_mode: 'center' or 'custom'
        pivot_x, pivot_y: Custom pivot position (pixels, when mode='custom')
        image_width, image_height: Input image dimensions (the geometry being
            transformed — the on-canvas frame hugs this, not the composite
            canvas, so a 1500×800 foreground frame sits at the lower-left of a
            1920×1080 canvas rather than stretching across the whole canvas).
        image_ndc_rect: The composite canvas rectangle in NDC (x0, y0, x1, y1)
            — i.e. where the whole canvas is displayed in the viewport after
            zoom/pan. Pixel→NDC scaling is derived from this rect and the
            canvas size.
        canvas_width, canvas_height: Composite canvas dimensions in pixels.
            When 0/missing, treated as equal to image_width/height (legacy
            single-image behaviour where the frame fills the canvas).

    Returns:
        TransformHandlesData with all handle positions in NDC.
    """
    # Fallback: when the canvas size isn't known, assume the image fills the
    # canvas (legacy behaviour). The math below then reduces to the original
    # centred-anchor form, so single-image scenes are unaffected.
    if canvas_width <= 0:
        canvas_width = image_width
    if canvas_height <= 0:
        canvas_height = image_height

    # Composite canvas rect in NDC (where the whole canvas is displayed).
    ndc_x0, ndc_y0, ndc_x1, ndc_y1 = image_ndc_rect
    ndc_w = ndc_x1 - ndc_x0
    ndc_h = ndc_y1 - ndc_y0

    # Pixel→NDC scale derived from the *canvas* (the displayed rect represents
    # the canvas, not the input image). Using image_width here was the bug:
    # for a 1500×800 image on a 1920×1080 canvas it stretched the frame across
    # the whole viewport.
    px_to_ndc_x = ndc_w / canvas_width if canvas_width > 0 else 1.0
    px_to_ndc_y = ndc_h / canvas_height if canvas_height > 0 else 1.0

    half_w = image_width / 2.0
    half_h = image_height / 2.0

    # Resolve pivot in pixel space
    if pivot_mode == "center":
        piv_px_x = half_w
        piv_px_y = half_h
    else:
        piv_px_x = pivot_x
        piv_px_y = pivot_y

    rad = math.radians(rotation)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)

    # Anchor = the input image's centre on the canvas (in NDC). At identity a
    # smaller input sits at the lower-left of the canvas; its centre maps to
    # (ndc_x0 + half_w * px_to_ndc_x, ndc_y0 + half_h * px_to_ndc_y). When
    # image == canvas this collapses to the canvas centre, preserving legacy
    # centred-anchor behaviour exactly.
    img_center_ndc_x = ndc_x0 + half_w * px_to_ndc_x
    img_center_ndc_y = ndc_y0 + half_h * px_to_ndc_y

    def to_ndc(px: float, py: float) -> Tuple[float, float]:
        """Transform an image-pixel point through the node transform → NDC.

        Works in Y-down image-pixel space (top-left origin) — the same space the
        renderer/shader uses (translate_y positive = down; standard rotation
        matrix, so a positive angle rotates clockwise on screen). Y is flipped to
        NDC's Y-up only at the final return. This must mirror TransformNode's
        _build_transform_matrix exactly so the handle overlay stays glued to the
        rendered image under any rotation/scale.
        """
        rel_x = px - piv_px_x
        rel_y = py - piv_px_y
        # scale (S) then rotate (R) — standard matrix in Y-down space
        sc_x = scale_x * rel_x
        sc_y = scale_y * rel_y
        rot_x = cos_r * sc_x - sin_r * sc_y
        rot_y = sin_r * sc_x + cos_r * sc_y
        # offset from image center, then translate (all in Y-down pixels)
        off_x = rot_x + (piv_px_x - half_w) + translate_x
        off_y = rot_y + (piv_px_y - half_h) + translate_y
        # Anchor at the image's centre on the canvas (not the canvas centre),
        # scaled by the canvas pixel→NDC ratio. NDC is Y-up: flip the Y offset.
        return (img_center_ndc_x + off_x * px_to_ndc_x,
                img_center_ndc_y - off_y * px_to_ndc_y)

    # Ordered transformed corners: TL, TR, BR, BL (image coords, y-down)
    c_tl = to_ndc(0.0, 0.0)
    c_tr = to_ndc(image_width, 0.0)
    c_br = to_ndc(image_width, image_height)
    c_bl = to_ndc(0.0, image_height)
    frame_corners = (c_tl, c_tr, c_br, c_bl)

    def _mid(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    # Edge midpoints sit on the rotated frame edges
    edge_top = _mid(c_tl, c_tr)      # image y=0 edge   → controls scale_y
    edge_bottom = _mid(c_bl, c_br)   # image y=H edge   → controls scale_y
    edge_left = _mid(c_tl, c_bl)     # image x=0 edge   → controls scale_x
    edge_right = _mid(c_tr, c_br)    # image x=W edge   → controls scale_x

    center = to_ndc(half_w, half_h)
    pivot = to_ndc(piv_px_x, piv_px_y)

    # Axis-aligned bbox of the rotated corners (kept for compatibility / hit extension)
    xs = [c_tl[0], c_tr[0], c_br[0], c_bl[0]]
    ys = [c_tl[1], c_tr[1], c_br[1], c_bl[1]]
    bbox_rect = (min(xs), min(ys), max(xs), max(ys))

    # Rotate handle sits directly above the pivot along the image's local
    # vertical axis — the center → top-edge direction — so the pivot → handle
    # line stays parallel to the frame's vertical edges no matter where the
    # pivot is. (It used to point from the pivot toward the top-middle point,
    # which went diagonal the moment the pivot moved off-center.)
    up_dx = edge_top[0] - center[0]
    up_dy = edge_top[1] - center[1]
    up_len = math.hypot(up_dx, up_dy)
    t = TRANSFORM_ROTATE_HANDLE_INSET
    if up_len > 1e-6:
        # (1 - t) of the half-height, matching the old offset when pivot==center.
        radius = (1.0 - t) * up_len
        rotate = (
            pivot[0] + (up_dx / up_len) * radius,
            pivot[1] + (up_dy / up_len) * radius,
        )
    else:
        rotate = pivot

    return TransformHandlesData(
        center=center,
        pivot=pivot,
        scale_x_left=edge_left,
        scale_x_right=edge_right,
        scale_y_top=edge_top,
        scale_y_bottom=edge_bottom,
        scale_corner_tl=c_tl,
        scale_corner_tr=c_tr,
        scale_corner_bl=c_bl,
        scale_corner_br=c_br,
        rotate=rotate,
        bbox_rect=bbox_rect,
        image_size=(image_width, image_height),
        frame_corners=frame_corners,
    )


def rotate_handle_ndc_following_mouse(
    home_ndc: Tuple[float, float],
    pivot_ndc: Tuple[float, float],
    mouse_ndc: Tuple[float, float],
    follow: bool,
) -> Tuple[float, float]:
    """Rotate-handle NDC, optionally orbiting the pivot toward the mouse.

    At rest the handle sits at ``home_ndc`` — the Fusion-style position on the
    top-edge → pivot guide, whose offset from the pivot always points at the
    frame's top-middle. That is why dragging it made the handle's perceived
    "orientation" always align to the top-middle.

    When ``follow`` is True (an active rotate drag), the handle instead orbits
    the pivot at the *home radius* in the direction of the mouse: same circle
    the home position lives on, rotated to face the cursor. The orbit is purely
    angular — the mouse's distance from the pivot is ignored — so the handle
    stays a constant, comfortable distance from the pivot regardless of how far
    the cursor roams. On release the caller stops passing ``follow`` and the
    handle snaps back to ``home_ndc``.

    The home radius ``|home_ndc - pivot_ndc|`` is invariant under rotation, so
    it does not jitter across a pure-rotate drag.

    Args:
        home_ndc: Resting rotate-handle position (NDC).
        pivot_ndc: Rotation/scale pivot (NDC).
        mouse_ndc: Current cursor position (NDC). Ignored when ``follow`` is False.
        follow: True while a rotate handle is being dragged.

    Returns:
        Effective rotate-handle position in NDC.
    """
    if not follow:
        return home_ndc

    hdx = home_ndc[0] - pivot_ndc[0]
    hdy = home_ndc[1] - pivot_ndc[1]
    radius = math.hypot(hdx, hdy)
    if radius < 1e-6:
        return home_ndc

    mdx = mouse_ndc[0] - pivot_ndc[0]
    mdy = mouse_ndc[1] - pivot_ndc[1]
    mlen = math.hypot(mdx, mdy)
    if mlen < 1e-6:
        # Cursor exactly on the pivot: angle undefined; keep home.
        return home_ndc

    return (
        pivot_ndc[0] + radius * (mdx / mlen),
        pivot_ndc[1] + radius * (mdy / mlen),
    )


def compute_cornerpin_handles(
    to_corners: List[Tuple[float, float]],
    from_corners: List[Tuple[float, float]],
    image_width: int,
    image_height: int,
    image_ndc_rect: Tuple[float, float, float, float],
) -> CornerPinHandlesData:
    """Compute handle positions for a CornerPin node.

    Args:
        to_corners: Destination corners in normalized (0-1) coordinates
                   [(to1_x, to1_y), (to2_x, to2_y), (to3_x, to3_y), (to4_x, to4_y)]
        from_corners: Source corners in normalized (0-1) coordinates
        image_width, image_height: Original image dimensions
        image_ndc_rect: The base image rectangle in NDC

    Returns:
        CornerPinHandlesData with all handle positions in NDC.
    """
    # Base image rect in NDC
    ndc_x0, ndc_y0, ndc_x1, ndc_y1 = image_ndc_rect
    ndc_w = ndc_x1 - ndc_x0
    ndc_h = ndc_y1 - ndc_y0

    # Convert normalized (0-1) to NDC
    # Note: normalized Y=0 is top, Y=1 is bottom; NDC Y is up-positive
    def norm_to_ndc(nx: float, ny: float) -> Tuple[float, float]:
        # X: 0->left, 1->right
        ndc_x = ndc_x0 + nx * ndc_w
        # Y: 0->top, 1->bottom -> NDC Y flips
        ndc_y = ndc_y1 - ny * ndc_h
        return (ndc_x, ndc_y)

    # Convert destination corners to NDC
    to_corners_ndc = [norm_to_ndc(x, y) for (x, y) in to_corners]

    # Convert source corners to NDC (these are reference, drawn semi-transparent)
    from_corners_ndc = [norm_to_ndc(x, y) for (x, y) in from_corners]

    return CornerPinHandlesData(
        to_corners=to_corners_ndc,
        from_corners=from_corners_ndc,
        image_size=(image_width, image_height),
    )


# ── Handle Definitions Generator ────────────────────────────────────────────

def get_transform_handle_definitions(
    handles_data: TransformHandlesData,
    hover_handle: Optional[str] = None,
    active_handle: Optional[str] = None,
) -> List[HandleDefinition]:
    """Generate HandleDefinition objects for Transform handles.

    Args:
        handles_data: Computed Transform handle positions
        hover_handle: Currently hovered handle ID
        active_handle: Currently active/dragging handle ID

    Returns:
        List of HandleDefinition objects ready for drawing.
    """
    definitions = []

    # Map handle IDs to their data
    handle_map = {
        TransformHandleType.CENTER: handles_data.center,
        TransformHandleType.PIVOT: handles_data.pivot,
        TransformHandleType.SCALE_X_L: handles_data.scale_x_left,
        TransformHandleType.SCALE_X_R: handles_data.scale_x_right,
        TransformHandleType.SCALE_Y_T: handles_data.scale_y_top,
        TransformHandleType.SCALE_Y_B: handles_data.scale_y_bottom,
        TransformHandleType.SCALE_CORNER_TL: handles_data.scale_corner_tl,
        TransformHandleType.SCALE_CORNER_TR: handles_data.scale_corner_tr,
        TransformHandleType.SCALE_CORNER_BL: handles_data.scale_corner_bl,
        TransformHandleType.SCALE_CORNER_BR: handles_data.scale_corner_br,
        TransformHandleType.ROTATE: handles_data.rotate,
    }

    # Color mapping for each handle type
    color_map = {
        TransformHandleType.CENTER: HANDLE_COLOR_CENTER,
        TransformHandleType.PIVOT: HANDLE_COLOR_CENTER,
        TransformHandleType.SCALE_X_L: HANDLE_COLOR_SCALE_X,
        TransformHandleType.SCALE_X_R: HANDLE_COLOR_SCALE_X,
        TransformHandleType.SCALE_Y_T: HANDLE_COLOR_SCALE_Y,
        TransformHandleType.SCALE_Y_B: HANDLE_COLOR_SCALE_Y,
        TransformHandleType.SCALE_CORNER_TL: HANDLE_COLOR_NORMAL,
        TransformHandleType.SCALE_CORNER_TR: HANDLE_COLOR_NORMAL,
        TransformHandleType.SCALE_CORNER_BL: HANDLE_COLOR_NORMAL,
        TransformHandleType.SCALE_CORNER_BR: HANDLE_COLOR_NORMAL,
        TransformHandleType.ROTATE: HANDLE_COLOR_ROTATE,
    }

    # Cursor hints for each handle type
    cursor_map = {
        TransformHandleType.CENTER: "size_all",
        TransformHandleType.PIVOT: "cross",
        TransformHandleType.SCALE_X_L: "size_hor",
        TransformHandleType.SCALE_X_R: "size_hor",
        TransformHandleType.SCALE_Y_T: "size_ver",
        TransformHandleType.SCALE_Y_B: "size_ver",
        TransformHandleType.SCALE_CORNER_TL: "size_fdiag",
        TransformHandleType.SCALE_CORNER_TR: "size_bdiag",
        TransformHandleType.SCALE_CORNER_BL: "size_bdiag",
        TransformHandleType.SCALE_CORNER_BR: "size_fdiag",
        TransformHandleType.ROTATE: "cross",
    }

    for handle_type, position in handle_map.items():
        handle_id = handle_type.value

        # Determine color based on state
        if handle_id == active_handle:
            color = HANDLE_COLOR_ACTIVE
        elif handle_id == hover_handle:
            color = HANDLE_COLOR_HOVER
        else:
            color = color_map.get(handle_type, HANDLE_COLOR_NORMAL)

        # Handle radius (rotate handle slightly larger for visibility)
        radius = 10.0 if handle_type == TransformHandleType.ROTATE else 8.0

        definitions.append(HandleDefinition(
            handle_type=handle_type,
            ndc_position=position,
            color=color,
            radius=radius,
            is_source=False,
            cursor_hint=cursor_map.get(handle_type, "size_all"),
        ))

    return definitions


def get_cornerpin_handle_definitions(
    handles_data: CornerPinHandlesData,
    hover_handle: Optional[str] = None,
    active_handle: Optional[str] = None,
) -> List[HandleDefinition]:
    """Generate HandleDefinition objects for CornerPin handles.

    Args:
        handles_data: Computed CornerPin handle positions
        hover_handle: Currently hovered handle ID
        active_handle: Currently active/dragging handle ID

    Returns:
        List of HandleDefinition objects ready for drawing.
    """
    definitions = []

    # Destination corners (to1-4)
    to_types = [
        CornerPinHandleType.TO_1,
        CornerPinHandleType.TO_2,
        CornerPinHandleType.TO_3,
        CornerPinHandleType.TO_4,
    ]

    for i, handle_type in enumerate(to_types):
        handle_id = handle_type.value
        position = handles_data.to_corners[i]

        # Determine color based on state
        if handle_id == active_handle:
            color = HANDLE_COLOR_ACTIVE
        elif handle_id == hover_handle:
            color = HANDLE_COLOR_HOVER
        else:
            color = HANDLE_COLOR_CORNERPIN

        definitions.append(HandleDefinition(
            handle_type=handle_type,
            ndc_position=position,
            color=color,
            radius=10.0,
            is_source=False,
            cursor_hint="size_all",
        ))

    # Source corners (from1-4) - semi-transparent reference
    from_types = [
        CornerPinHandleType.FROM_1,
        CornerPinHandleType.FROM_2,
        CornerPinHandleType.FROM_3,
        CornerPinHandleType.FROM_4,
    ]

    for i, handle_type in enumerate(from_types):
        handle_id = handle_type.value
        position = handles_data.from_corners[i]

        # Source handles are always semi-transparent (except when active)
        if handle_id == active_handle:
            color = HANDLE_COLOR_ACTIVE
        elif handle_id == hover_handle:
            color = (HANDLE_COLOR_HOVER[0], HANDLE_COLOR_HOVER[1],
                     HANDLE_COLOR_HOVER[2], 0.6)
        else:
            color = HANDLE_COLOR_SOURCE

        definitions.append(HandleDefinition(
            handle_type=handle_type,
            ndc_position=position,
            color=color,
            radius=8.0,
            is_source=True,
            cursor_hint="size_all",
        ))

    return definitions


# ── Coordinate Conversion for Handles ───────────────────────────────────────

def ndc_to_screen_pixels(
    ndc_pos: Tuple[float, float],
    viewport_width: float,
    viewport_height: float,
    device_pixel_ratio: float = 1.0,
) -> Tuple[float, float]:
    """Convert NDC position to screen pixel coordinates.

    Args:
        ndc_pos: Position in NDC (-1 to 1)
        viewport_width, viewport_height: Widget dimensions in pixels
        device_pixel_ratio: Screen DPR

    Returns:
        (x, y) in screen pixels (top-left origin).
    """
    vp_w = viewport_width * device_pixel_ratio
    vp_h = viewport_height * device_pixel_ratio

    # NDC to pixel: x = (ndc_x + 1) * vp_w / 2, y = (1 - ndc_y) * vp_h / 2
    px_x = (ndc_pos[0] + 1.0) * vp_w / 2.0 / device_pixel_ratio
    px_y = (1.0 - ndc_pos[1]) * vp_h / 2.0 / device_pixel_ratio

    return (px_x, px_y)


def screen_pixels_to_ndc(
    screen_pos: Tuple[float, float],
    viewport_width: float,
    viewport_height: float,
    device_pixel_ratio: float = 1.0,
) -> Tuple[float, float]:
    """Convert screen pixel coordinates to NDC.

    Args:
        screen_pos: Position in screen pixels (top-left origin)
        viewport_width, viewport_height: Widget dimensions
        device_pixel_ratio: Screen DPR

    Returns:
        (x, y) in NDC (-1 to 1).
    """
    vp_w = viewport_width * device_pixel_ratio
    vp_h = viewport_height * device_pixel_ratio

    px_dpr_x = screen_pos[0] * device_pixel_ratio
    px_dpr_y = screen_pos[1] * device_pixel_ratio

    ndc_x = (px_dpr_x / (vp_w / 2.0)) - 1.0
    ndc_y = 1.0 - (px_dpr_y / (vp_h / 2.0))

    return (ndc_x, ndc_y)


# ── Hit Testing Utilities ────────────────────────────────────────────────────

def hit_test_handle_screen(
    screen_pos: QPointF,
    handle_screen_pos: Tuple[float, float],
    hit_radius: float = 10.0,
) -> bool:
    """Test if screen position hits a handle using screen-pixel distance.

    This ensures consistent hit area regardless of zoom level.

    Args:
        screen_pos: Current mouse position in screen pixels
        handle_screen_pos: Handle position in screen pixels
        hit_radius: Hit radius in screen pixels

    Returns:
        True if within hit radius.
    """
    dx = screen_pos.x() - handle_screen_pos[0]
    dy = screen_pos.y() - handle_screen_pos[1]
    dist_sq = dx * dx + dy * dy
    return dist_sq <= (hit_radius * hit_radius)


def find_nearest_handle(
    screen_pos: QPointF,
    handle_definitions: List[HandleDefinition],
    viewport_width: float,
    viewport_height: float,
    device_pixel_ratio: float = 1.0,
    hit_radius: float = 12.0,
) -> Optional[str]:
    """Find the nearest handle to the given screen position.

    Args:
        screen_pos: Current mouse position in screen pixels
        handle_definitions: List of HandleDefinition objects
        viewport_width, viewport_height: Widget dimensions
        device_pixel_ratio: Screen DPR
        hit_radius: Maximum hit distance in screen pixels

    Returns:
        Handle ID (handle_type.value) if hit, None otherwise.
    """
    best_handle = None
    best_dist_sq = hit_radius * hit_radius

    for handle_def in handle_definitions:
        # Convert handle NDC to screen pixels
        handle_screen = ndc_to_screen_pixels(
            handle_def.ndc_position,
            viewport_width,
            viewport_height,
            device_pixel_ratio,
        )

        dx = screen_pos.x() - handle_screen[0]
        dy = screen_pos.y() - handle_screen[1]
        dist_sq = dx * dx + dy * dy

        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_handle = handle_def.handle_type.value

    return best_handle


# ── Parameter Calculation Helpers ──────────────────────────────────────────

def compute_transform_delta_params(
    handle_id: str,
    drag_start_image_pos: Tuple[float, float],
    drag_current_image_pos: Tuple[float, float],
    original_params: Dict[str, float],
    image_width: int,
    image_height: int,
    modifiers: int = 0,
    max_scale: float = 5.0,
) -> Dict[str, float]:
    """Compute new transform parameters based on handle drag (rotation-aware).

    Scale/rotate are recomputed **absolutely** from the current mouse position
    (not as an incremental delta), in the image's local (unrotated) frame, so they
    stay correct under any rotation and never drift. Corner/edge drags keep the
    opposite anchor fixed in world space (Fusion default).

    Args:
        handle_id: The handle being dragged ("center"/"move", "pivot",
            "rotate", "scale_corner_*", "scale_x_*", "scale_y_*").
        drag_start_image_pos: Initial position in image pixels.
        drag_current_image_pos: Current position in image pixels.
        original_params: Original node parameters before drag.
        image_width, image_height: Image dimensions.
        modifiers: Keyboard modifiers (default locks aspect ratio on corners;
            Shift frees the axes for independent x/y scaling).

    Returns:
        Dict of parameter names to new values.
    """
    # Normalize modifiers: callers may pass a raw int bitmask OR a PyQt6
    # Qt.KeyboardModifier flag enum. PyQt6's strict enums reject int(), which
    # crashed every corner-scale drag (affects_x and affects_y are both True for
    # corners, so the Shift-uniform check below always evaluated int(modifiers)).
    # AUD-11306: the cast lives in the shared _normalize_modifiers helper so
    # the mask/roto delta functions get the same defense.
    modifiers = _normalize_modifiers(modifiers)

    new_params = {}

    # Original parameter values
    orig_tx = original_params.get("translate_x", 0.0)
    orig_ty = original_params.get("translate_y", 0.0)
    orig_sx = original_params.get("scale_x", 1.0)
    orig_sy = original_params.get("scale_y", 1.0)
    orig_rot = original_params.get("rotation", 0.0)
    orig_pivot_mode = original_params.get("pivot_mode", "center")
    orig_piv_x = original_params.get("pivot_x", image_width / 2.0)
    orig_piv_y = original_params.get("pivot_y", image_height / 2.0)

    half_w = image_width / 2.0
    half_h = image_height / 2.0

    # Resolve pivot in pre-transform pixel space
    if orig_pivot_mode == "center":
        piv_x = half_w
        piv_y = half_h
    else:
        piv_x = orig_piv_x
        piv_y = orig_piv_y

    if handle_id == "move":
        handle_id = TransformHandleType.CENTER.value

    handle_type = TransformHandleType(handle_id) if handle_id in [h.value for h in TransformHandleType] else None
    if handle_type is None:
        return new_params

    cur_x, cur_y = drag_current_image_pos[0], drag_current_image_pos[1]
    start_x, start_y = drag_start_image_pos[0], drag_start_image_pos[1]

    SCALE_TYPES = (
        TransformHandleType.SCALE_X_L, TransformHandleType.SCALE_X_R,
        TransformHandleType.SCALE_Y_T, TransformHandleType.SCALE_Y_B,
        TransformHandleType.SCALE_CORNER_TL, TransformHandleType.SCALE_CORNER_TR,
        TransformHandleType.SCALE_CORNER_BL, TransformHandleType.SCALE_CORNER_BR,
    )

    if handle_type == TransformHandleType.CENTER:
        # Translate by the raw mouse delta. Both the mouse positions and the
        # renderer's translate are in Y-down image space (translate_y positive =
        # down), so no Y flip is needed — the image follows the cursor directly.
        new_params["translate_x"] = orig_tx + (cur_x - start_x)
        new_params["translate_y"] = orig_ty + (cur_y - start_y)

    elif handle_type == TransformHandleType.PIVOT:
        # Move pivot in pre-transform space; switch to custom mode.
        # Moving the pivot does not move the image — only the rotation/scale origin.
        # Pivot coordinates are in image pixel space (Y-down).
        new_params["pivot_mode"] = "custom"
        new_params["pivot_x"] = piv_x + (cur_x - start_x)
        new_params["pivot_y"] = piv_y + (cur_y - start_y)

    elif handle_type == TransformHandleType.ROTATE:
        # Rotation = the mouse's angle around the pivot, measured clockwise from
        # straight-up (screen space, Y-down). The rotate guide points up, so the
        # mouse directly above the pivot = 0°; dragging clockwise (to the right)
        # increases the angle. The renderer rotates clockwise for a positive angle
        # (standard matrix in Y-down space), so this matches it and the image
        # follows the handle.
        piv_world_x = piv_x + orig_tx
        piv_world_y = piv_y + orig_ty  # Y-down; the pivot is the transform's fixed point

        dx = cur_x - piv_world_x
        dy = cur_y - piv_world_y       # Y-down: positive = below pivot on screen
        rad = math.atan2(dx, -dy)      # clockwise-from-up angle
        new_params["rotation"] = _wrap_180(math.degrees(rad))

    elif handle_type in SCALE_TYPES:
        # Opposite-anchor-fixed scaling (Fusion default), rotation-aware.
        # The opposite corner/edge stays at its current world position while the
        # dragged handle tracks the mouse. Work in the image's local (unrotated)
        # frame relative to the anchor, all in Y-down image space (same as the
        # renderer):
        #   new_scale = local(mouse − anchor_world) / (handle_local − anchor_local)
        h_local, a_local = _handle_local_anchor(handle_id, image_width, image_height)

        rad = math.radians(orig_rot)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        def _world(px: float, py: float) -> Tuple[float, float]:
            """Current world position of pre-transform point p under original params."""
            rx, ry = px - piv_x, py - piv_y
            scx, scy = orig_sx * rx, orig_sy * ry
            wx = cos_r * scx - sin_r * scy
            wy = sin_r * scx + cos_r * scy
            return (wx + piv_x + orig_tx, wy + piv_y + orig_ty)

        anchor_world = _world(a_local[0], a_local[1])

        # Un-rotate (mouse − anchor_world) into local space: local = R(−θ) · world
        mx = cur_x - anchor_world[0]
        my = cur_y - anchor_world[1]
        lx = cos_r * mx + sin_r * my
        ly = -sin_r * mx + cos_r * my

        affects_x = handle_type in (
            TransformHandleType.SCALE_X_L, TransformHandleType.SCALE_X_R,
            TransformHandleType.SCALE_CORNER_TL, TransformHandleType.SCALE_CORNER_TR,
            TransformHandleType.SCALE_CORNER_BL, TransformHandleType.SCALE_CORNER_BR,
        )
        affects_y = handle_type in (
            TransformHandleType.SCALE_Y_T, TransformHandleType.SCALE_Y_B,
            TransformHandleType.SCALE_CORNER_TL, TransformHandleType.SCALE_CORNER_TR,
            TransformHandleType.SCALE_CORNER_BL, TransformHandleType.SCALE_CORNER_BR,
        )

        # Original extent from anchor to handle, per axis (pre-transform, Y-down)
        ext_x = h_local[0] - a_local[0]
        ext_y = h_local[1] - a_local[1]

        sx_new = orig_sx
        sy_new = orig_sy
        if affects_x and abs(ext_x) > 1e-6:
            sx_new = max(0.01, min(max_scale, lx / ext_x))
        if affects_y and abs(ext_y) > 1e-6:
            sy_new = max(0.01, min(max_scale, ly / ext_y))

        # Default (no modifier) = uniform scale on corner handles, preserving the
        # original aspect ratio; Shift frees the axes for independent x/y scaling.
        # Uniform uses the average of the independently-computed per-axis scales.
        if affects_x and affects_y and not (modifiers & _SHIFT_MODIFIER_MASK):
            avg = (sx_new + sy_new) / 2.0
            sx_new = sy_new = max(0.01, min(max_scale, avg))

        # Translate so the opposite anchor stays fixed in world space:
        #   t_new = anchor_world − (R·S_new·(anchor_local − piv) + piv)
        arx, ary = a_local[0] - piv_x, a_local[1] - piv_y
        ascx, ascy = sx_new * arx, sy_new * ary
        awx = cos_r * ascx - sin_r * ascy
        awy = sin_r * ascx + cos_r * ascy

        new_params["scale_x"] = sx_new
        new_params["scale_y"] = sy_new
        new_params["translate_x"] = anchor_world[0] - (awx + piv_x)
        new_params["translate_y"] = anchor_world[1] - (awy + piv_y)

    return new_params


# DORMANT(2026-10-01 审计 AUD-11307): 无任何生产调用方（CompositePage 在页内
# 内联实现 CornerPin 拖拽增量公式）。与页内实现是双轨，易被误用；接线前请勿
# 在生产依赖。若长期无接线计划建议删除。
def compute_cornerpin_delta_params(
    handle_id: str,
    drag_start_image_pos: Tuple[float, float],
    drag_current_image_pos: Tuple[float, float],
    original_params: Dict[str, float],
    image_width: int,
    image_height: int,
) -> Dict[str, float]:
    """Compute new cornerpin parameters based on corner drag.

    Args:
        handle_id: The corner handle (e.g., "to1", "to2", etc.)
        drag_start_image_pos: Initial position in image pixels
        drag_current_image_pos: Current position in image pixels
        original_params: Original node parameters
        image_width, image_height: Image dimensions

    Returns:
        Dict of parameter names to new values.
    """
    # Convert image pixels to normalized (0-1) coordinates
    nx = drag_current_image_pos[0] / image_width
    ny = drag_current_image_pos[1] / image_height

    new_params = {}

    handle_type = CornerPinHandleType(handle_id) if handle_id in [h.value for h in CornerPinHandleType] else None

    if handle_type is None:
        return new_params

    if handle_type == CornerPinHandleType.TO_1:
        new_params["to1_x"] = nx
        new_params["to1_y"] = ny
    elif handle_type == CornerPinHandleType.TO_2:
        new_params["to2_x"] = nx
        new_params["to2_y"] = ny
    elif handle_type == CornerPinHandleType.TO_3:
        new_params["to3_x"] = nx
        new_params["to3_y"] = ny
    elif handle_type == CornerPinHandleType.TO_4:
        new_params["to4_x"] = nx
        new_params["to4_y"] = ny
    elif handle_type == CornerPinHandleType.FROM_1:
        new_params["from1_x"] = nx
        new_params["from1_y"] = ny
    elif handle_type == CornerPinHandleType.FROM_2:
        new_params["from2_x"] = nx
        new_params["from2_y"] = ny
    elif handle_type == CornerPinHandleType.FROM_3:
        new_params["from3_x"] = nx
        new_params["from3_y"] = ny
    elif handle_type == CornerPinHandleType.FROM_4:
        new_params["from4_x"] = nx
        new_params["from4_y"] = ny

    return new_params


# ── Mask/Roto Handle Data Structures (S5-11) ─────────────────────────────────────

@dataclass
class MaskHandlesData:
    """Computed handle positions for a Mask/RotoShape node.

    All positions are in NDC coordinates relative to the current viewport.
    The shape is defined by center, width, height, rotation, and feather.
    """
    center: Tuple[float, float]             # Center crosshair position (NDC)
    rotate: Tuple[float, float]             # Rotation handle position (NDC, outside bbox)
    width_left: Tuple[float, float]         # Left edge (width control)
    width_right: Tuple[float, float]        # Right edge (width control)
    height_top: Tuple[float, float]         # Top edge (height control)
    height_bottom: Tuple[float, float]      # Bottom edge (height control)
    corner_tl: Tuple[float, float]          # Top-left corner
    corner_tr: Tuple[float, float]          # Top-right corner
    corner_bl: Tuple[float, float]          # Bottom-left corner
    corner_br: Tuple[float, float]          # Bottom-right corner
    feather_t: Tuple[float, float]          # Top feather edge
    feather_b: Tuple[float, float]          # Bottom feather edge
    feather_l: Tuple[float, float]          # Left feather edge
    feather_r: Tuple[float, float]          # Right feather edge
    bbox_rect: Tuple[float, float, float, float]  # Shape bbox in NDC (x0, y0, x1, y1)
    image_size: Tuple[int, int]             # Original image dimensions
    shape_type: str                         # "rectangle", "ellipse", "polygon"
    polygon_radius: Optional[Tuple[float, float]] = None  # Polygon radius handle


@dataclass
class RotoHandlesData:
    """Computed handle positions for a RotoShape node.

    Extends MaskHandlesData with opacity control.
    """
    center: Tuple[float, float]
    rotate: Tuple[float, float]
    width_left: Tuple[float, float]
    width_right: Tuple[float, float]
    height_top: Tuple[float, float]
    height_bottom: Tuple[float, float]
    corner_tl: Tuple[float, float]
    corner_tr: Tuple[float, float]
    corner_bl: Tuple[float, float]
    corner_br: Tuple[float, float]
    feather_t: Tuple[float, float]
    feather_b: Tuple[float, float]
    feather_l: Tuple[float, float]
    feather_r: Tuple[float, float]
    bbox_rect: Tuple[float, float, float, float]
    image_size: Tuple[int, int]
    shape_type: str
    polygon_radius: Optional[Tuple[float, float]] = None
    opacity: Optional[Tuple[float, float]] = None  # Opacity slider control position


# ── Mask/Roto Handle Position Calculators (S5-11) ───────────────────────────────

def compute_mask_handles(
    mask_type: str,
    center_x: float,
    center_y: float,
    width_norm: float,
    height_norm: float,
    rotation: float,
    feather: float,
    image_width: int,
    image_height: int,
    image_ndc_rect: Tuple[float, float, float, float],
) -> MaskHandlesData:
    """Compute all handle positions for a Mask/RotoShape node.

    MaskNode parameters:
    - center_x, center_y: Normalized (0-1) center position
    - width, height: Normalized (0-2) size relative to image
    - rotation: Degrees (-180 to 180)
    - feather: Normalized (0-0.5 for both MaskNode and RotoShapeNode —
      AUD-11311: RotoShapeNode's ParamDef clamps feather to 0.5 too)

    Args:
        mask_type: "rectangle", "ellipse", or "polygon"
        center_x, center_y: Normalized center position (0-1)
        width_norm, height_norm: Normalized size (0-2)
        rotation: Rotation in degrees
        feather: Normalized feather amount
        image_width, image_height: Original image dimensions
        image_ndc_rect: The base image rectangle in NDC (x0, y0, x1, y1)

    Returns:
        MaskHandlesData with all handle positions in NDC.
    """
    # Base image rect in NDC
    ndc_x0, ndc_y0, ndc_x1, ndc_y1 = image_ndc_rect
    ndc_w = ndc_x1 - ndc_x0
    ndc_h = ndc_y1 - ndc_y0

    # Convert normalized (0-1) center to NDC
    # Note: center_x/y are relative to image (0-1), NDC Y is up-positive
    center_ndc_x = ndc_x0 + center_x * ndc_w
    center_ndc_y = ndc_y1 - center_y * ndc_h  # Y flip: image Y-down to NDC Y-up

    # Convert normalized size (0-2) to NDC dimensions
    # width_norm=1.0 means full width, so shape width = width_norm * image_width
    shape_w_ndc = width_norm * ndc_w / 2.0  # Half-width in NDC
    shape_h_ndc = height_norm * ndc_h / 2.0  # Half-height in NDC

    # Rotation in radians
    rad = math.radians(rotation)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)

    # Compute bbox corners (unrotated first, then rotate around center)
    # Corners relative to center: (-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)
    corners_rel = [
        (-shape_w_ndc, -shape_h_ndc),  # Bottom-left (relative to center)
        ( shape_w_ndc, -shape_h_ndc),  # Bottom-right
        ( shape_w_ndc,  shape_h_ndc),  # Top-right
        (-shape_w_ndc,  shape_h_ndc),  # Top-left
    ]

    # Apply rotation to corners
    corners_rotated = []
    for (cx_rel, cy_rel) in corners_rel:
        # Rotate around center
        rx = cx_rel * cos_r - cy_rel * sin_r
        ry = cx_rel * sin_r + cy_rel * cos_r
        # Translate to absolute NDC position
        abs_x = center_ndc_x + rx
        abs_y = center_ndc_y + ry
        corners_rotated.append((abs_x, abs_y))

    # Extract rotated corners (BL, BR, TR, TL)
    bl = corners_rotated[0]
    br = corners_rotated[1]
    tr = corners_rotated[2]
    tl = corners_rotated[3]

    # Compute bbox from rotated corners
    bbox_x0 = min(c[0] for c in corners_rotated)
    bbox_y0 = min(c[1] for c in corners_rotated)
    bbox_x1 = max(c[0] for c in corners_rotated)
    bbox_y1 = max(c[1] for c in corners_rotated)
    bbox_rect = (bbox_x0, bbox_y0, bbox_x1, bbox_y1)

    # Center of bbox (for center handle)
    # The center handle is at the shape center (not bbox center, but they're the same for centered shapes)
    center_pos = (center_ndc_x, center_ndc_y)

    # Edge handles (center of each edge, rotated)
    # For simplicity, compute edge centers in rotated coordinates
    # Left edge center: average of TL and BL
    width_left = ((tl[0] + bl[0]) / 2.0, (tl[1] + bl[1]) / 2.0)
    # Right edge center: average of TR and BR
    width_right = ((tr[0] + br[0]) / 2.0, (tr[1] + br[1]) / 2.0)
    # Top edge center: average of TL and TR
    height_top = ((tl[0] + tr[0]) / 2.0, (tl[1] + tr[1]) / 2.0)
    # Bottom edge center: average of BL and BR
    height_bottom = ((bl[0] + br[0]) / 2.0, (bl[1] + br[1]) / 2.0)

    # Corner positions (already computed)
    corner_tl_pos = tl
    corner_tr_pos = tr
    corner_bl_pos = bl
    corner_br_pos = br

    # Rotate handle (outside bbox, typically above top-center)
    # Fixed NDC offset from bbox top edge
    rotate_offset_ndc = 0.08  # Fixed visual offset
    rotate_pos = (center_ndc_x, bbox_y1 + rotate_offset_ndc)

    # Feather handles (offset from edges by feather amount)
    # Feather is normalized (0-0.5 for MaskNode), convert to NDC offset
    # For rectangle/ellipse, feather applies uniformly from edge inward
    feather_ndc = feather * ndc_w  # Approximate feather in NDC

    # Feather handles are positioned outside the shape edge, along the edge direction
    # For simplicity, place them at the edge positions + feather offset outward
    # Top feather: above top edge center
    # Compute outward direction for each edge (perpendicular to edge, pointing outward)
    # For rotated shapes, compute edge normal directions

    # Edge normals (pointing outward from shape)
    # AUD-11301: rotate the axis normals with the SAME matrix as the corners
    # above (R(+θ) in NDC Y-up). The old values were R(−θ) rotations, so any
    # rotated mask had its four feather handles mirrored to the wrong side
    # of the shape.
    top_normal = (-sin_r, cos_r)  # R(+θ)·(0, 1), pointing up when θ=0
    bottom_normal = (sin_r, -cos_r)  # R(+θ)·(0, -1)
    left_normal = (-cos_r, -sin_r)  # R(+θ)·(-1, 0)
    right_normal = (cos_r, sin_r)  # R(+θ)·(1, 0)

    # Feather handle positions: edge center + normal * feather_offset
    feather_t_pos = (
        height_top[0] + top_normal[0] * feather_ndc,
        height_top[1] + top_normal[1] * feather_ndc,
    )
    feather_b_pos = (
        height_bottom[0] + bottom_normal[0] * feather_ndc,
        height_bottom[1] + bottom_normal[1] * feather_ndc,
    )
    feather_l_pos = (
        width_left[0] + left_normal[0] * feather_ndc,
        width_left[1] + left_normal[1] * feather_ndc,
    )
    feather_r_pos = (
        width_right[0] + right_normal[0] * feather_ndc,
        width_right[1] + right_normal[1] * feather_ndc,
    )

    # Polygon radius handle (for polygon shapes)
    polygon_radius_pos = None
    if mask_type == "polygon":
        # Radius handle is at a vertex direction from center
        # For a regular polygon, place it at the first vertex
        # The radius is essentially the "half-width" for polygons
        # Place it at the right-most vertex (before rotation)
        radius_rel = (shape_w_ndc, 0.0)
        # Rotate
        radius_rot_x = radius_rel[0] * cos_r - radius_rel[1] * sin_r
        radius_rot_y = radius_rel[0] * sin_r + radius_rel[1] * cos_r
        polygon_radius_pos = (
            center_ndc_x + radius_rot_x,
            center_ndc_y + radius_rot_y,
        )

    return MaskHandlesData(
        center=center_pos,
        rotate=rotate_pos,
        width_left=width_left,
        width_right=width_right,
        height_top=height_top,
        height_bottom=height_bottom,
        corner_tl=corner_tl_pos,
        corner_tr=corner_tr_pos,
        corner_bl=corner_bl_pos,
        corner_br=corner_br_pos,
        feather_t=feather_t_pos,
        feather_b=feather_b_pos,
        feather_l=feather_l_pos,
        feather_r=feather_r_pos,
        bbox_rect=bbox_rect,
        image_size=(image_width, image_height),
        shape_type=mask_type,
        polygon_radius=polygon_radius_pos,
    )


def compute_roto_handles(
    shape_type: str,
    center_x: float,
    center_y: float,
    width_norm: float,
    height_norm: float,
    rotation: float,
    feather: float,
    opacity: float,
    image_width: int,
    image_height: int,
    image_ndc_rect: Tuple[float, float, float, float],
) -> RotoHandlesData:
    """Compute all handle positions for a RotoShape node.

    Extends Mask handles with opacity control.

    Args:
        shape_type: "rectangle", "ellipse", or "polygon"
        center_x, center_y: Normalized center position (0-1)
        width_norm, height_norm: Normalized size (0-2)
        rotation: Rotation in degrees
        feather: Normalized feather amount (0-0.5 — RotoShapeNode clamps
            feather to 0.5, see AUD-11311)
        opacity: Opacity value (0-1)
        image_width, image_height: Original image dimensions
        image_ndc_rect: The base image rectangle in NDC

    Returns:
        RotoHandlesData with all handle positions in NDC.
    """
    # First compute mask handles (same geometry)
    mask_data = compute_mask_handles(
        shape_type, center_x, center_y, width_norm, height_norm,
        rotation, feather, image_width, image_height, image_ndc_rect
    )

    # Opacity handle position (optional)
    # Place it below the shape, similar to rotate handle above
    opacity_pos = None
    if opacity < 1.0:
        # Show opacity handle when opacity is not 1.0
        # Position it below the bottom edge center
        opacity_offset_ndc = 0.08
        opacity_pos = (
            mask_data.center[0],
            mask_data.bbox_rect[1] - opacity_offset_ndc  # Below bbox bottom
        )

    return RotoHandlesData(
        center=mask_data.center,
        rotate=mask_data.rotate,
        width_left=mask_data.width_left,
        width_right=mask_data.width_right,
        height_top=mask_data.height_top,
        height_bottom=mask_data.height_bottom,
        corner_tl=mask_data.corner_tl,
        corner_tr=mask_data.corner_tr,
        corner_bl=mask_data.corner_bl,
        corner_br=mask_data.corner_br,
        feather_t=mask_data.feather_t,
        feather_b=mask_data.feather_b,
        feather_l=mask_data.feather_l,
        feather_r=mask_data.feather_r,
        bbox_rect=mask_data.bbox_rect,
        image_size=mask_data.image_size,
        shape_type=shape_type,
        polygon_radius=mask_data.polygon_radius,
        opacity=opacity_pos,
    )


# ── Mask/Roto Handle Definition Generators (S5-11) ─────────────────────────────

def get_mask_handle_definitions(
    handles_data: MaskHandlesData,
    hover_handle: Optional[str] = None,
    active_handle: Optional[str] = None,
) -> List[HandleDefinition]:
    """Generate HandleDefinition objects for Mask handles.

    DaVinci-style visual appearance:
    - Border: dark gray (60, 60, 60)
    - Fill: white
    - Feather/Rotate: cyan (0, 255, 255)
    - Center: crosshair

    Args:
        handles_data: Computed Mask handle positions
        hover_handle: Currently hovered handle ID
        active_handle: Currently active/dragging handle ID

    Returns:
        List of HandleDefinition objects ready for drawing.
    """
    definitions = []

    # Map handle types to positions
    handle_map = {
        MaskHandleType.CENTER: handles_data.center,
        MaskHandleType.ROTATE: handles_data.rotate,
        MaskHandleType.WIDTH_L: handles_data.width_left,
        MaskHandleType.WIDTH_R: handles_data.width_right,
        MaskHandleType.HEIGHT_T: handles_data.height_top,
        MaskHandleType.HEIGHT_B: handles_data.height_bottom,
        MaskHandleType.CORNER_TL: handles_data.corner_tl,
        MaskHandleType.CORNER_TR: handles_data.corner_tr,
        MaskHandleType.CORNER_BL: handles_data.corner_bl,
        MaskHandleType.CORNER_BR: handles_data.corner_br,
        MaskHandleType.FEATHER_T: handles_data.feather_t,
        MaskHandleType.FEATHER_B: handles_data.feather_b,
        MaskHandleType.FEATHER_L: handles_data.feather_l,
        MaskHandleType.FEATHER_R: handles_data.feather_r,
    }

    if handles_data.polygon_radius is not None:
        handle_map[MaskHandleType.POLYGON_RADIUS] = handles_data.polygon_radius

    # Color mapping (DaVinci style)
    color_map = {
        MaskHandleType.CENTER: MASK_COLOR_CENTER,
        MaskHandleType.ROTATE: MASK_COLOR_ROTATE,
        MaskHandleType.WIDTH_L: MASK_COLOR_WIDTH,
        MaskHandleType.WIDTH_R: MASK_COLOR_WIDTH,
        MaskHandleType.HEIGHT_T: MASK_COLOR_HEIGHT,
        MaskHandleType.HEIGHT_B: MASK_COLOR_HEIGHT,
        MaskHandleType.CORNER_TL: MASK_COLOR_CORNER,
        MaskHandleType.CORNER_TR: MASK_COLOR_CORNER,
        MaskHandleType.CORNER_BL: MASK_COLOR_CORNER,
        MaskHandleType.CORNER_BR: MASK_COLOR_CORNER,
        MaskHandleType.FEATHER_T: MASK_COLOR_FEATHER,
        MaskHandleType.FEATHER_B: MASK_COLOR_FEATHER,
        MaskHandleType.FEATHER_L: MASK_COLOR_FEATHER,
        MaskHandleType.FEATHER_R: MASK_COLOR_FEATHER,
        MaskHandleType.POLYGON_RADIUS: MASK_COLOR_FILL,
    }

    # Cursor hints for each handle type
    cursor_map = {
        MaskHandleType.CENTER: "size_all",
        MaskHandleType.ROTATE: "cross",
        MaskHandleType.WIDTH_L: "size_hor",
        MaskHandleType.WIDTH_R: "size_hor",
        MaskHandleType.HEIGHT_T: "size_ver",
        MaskHandleType.HEIGHT_B: "size_ver",
        MaskHandleType.CORNER_TL: "size_fdiag",
        MaskHandleType.CORNER_TR: "size_bdiag",
        MaskHandleType.CORNER_BL: "size_bdiag",
        MaskHandleType.CORNER_BR: "size_fdiag",
        MaskHandleType.FEATHER_T: "cross",
        MaskHandleType.FEATHER_B: "cross",
        MaskHandleType.FEATHER_L: "cross",
        MaskHandleType.FEATHER_R: "cross",
        MaskHandleType.POLYGON_RADIUS: "size_all",
    }

    # Handle sizes (screen pixels)
    # Center: larger (11px), Rotate: 9px, Others: 8px, Feather: 7px
    size_map = {
        MaskHandleType.CENTER: 11.0,
        MaskHandleType.ROTATE: 9.0,
        MaskHandleType.FEATHER_T: 7.0,
        MaskHandleType.FEATHER_B: 7.0,
        MaskHandleType.FEATHER_L: 7.0,
        MaskHandleType.FEATHER_R: 7.0,
    }

    for handle_type, position in handle_map.items():
        handle_id = handle_type.value

        # Determine color based on state
        if handle_id == active_handle:
            color = HANDLE_COLOR_ACTIVE
        elif handle_id == hover_handle:
            color = HANDLE_COLOR_HOVER
        else:
            color = color_map.get(handle_type, MASK_COLOR_FILL)

        # Handle radius
        radius = size_map.get(handle_type, 8.0)

        definitions.append(HandleDefinition(
            handle_type=handle_type,
            ndc_position=position,
            color=color,
            radius=radius,
            is_source=False,
            cursor_hint=cursor_map.get(handle_type, "size_all"),
        ))

    return definitions


def get_roto_handle_definitions(
    handles_data: RotoHandlesData,
    hover_handle: Optional[str] = None,
    active_handle: Optional[str] = None,
) -> List[HandleDefinition]:
    """Generate HandleDefinition objects for Roto handles.

    Extends Mask handles with opacity control.

    Args:
        handles_data: Computed Roto handle positions
        hover_handle: Currently hovered handle ID
        active_handle: Currently active/dragging handle ID

    Returns:
        List of HandleDefinition objects ready for drawing.
    """
    definitions = []

    # First get mask-style handles
    mask_data = MaskHandlesData(
        center=handles_data.center,
        rotate=handles_data.rotate,
        width_left=handles_data.width_left,
        width_right=handles_data.width_right,
        height_top=handles_data.height_top,
        height_bottom=handles_data.height_bottom,
        corner_tl=handles_data.corner_tl,
        corner_tr=handles_data.corner_tr,
        corner_bl=handles_data.corner_bl,
        corner_br=handles_data.corner_br,
        feather_t=handles_data.feather_t,
        feather_b=handles_data.feather_b,
        feather_l=handles_data.feather_l,
        feather_r=handles_data.feather_r,
        bbox_rect=handles_data.bbox_rect,
        image_size=handles_data.image_size,
        shape_type=handles_data.shape_type,
        polygon_radius=handles_data.polygon_radius,
    )

    # Convert mask definitions to roto definitions
    mask_defs = get_mask_handle_definitions(mask_data, hover_handle, active_handle)

    # Convert MaskHandleType to RotoHandleType
    type_conversion = {
        MaskHandleType.CENTER: RotoHandleType.CENTER,
        MaskHandleType.ROTATE: RotoHandleType.ROTATE,
        MaskHandleType.WIDTH_L: RotoHandleType.WIDTH_L,
        MaskHandleType.WIDTH_R: RotoHandleType.WIDTH_R,
        MaskHandleType.HEIGHT_T: RotoHandleType.HEIGHT_T,
        MaskHandleType.HEIGHT_B: RotoHandleType.HEIGHT_B,
        MaskHandleType.CORNER_TL: RotoHandleType.CORNER_TL,
        MaskHandleType.CORNER_TR: RotoHandleType.CORNER_TR,
        MaskHandleType.CORNER_BL: RotoHandleType.CORNER_BL,
        MaskHandleType.CORNER_BR: RotoHandleType.CORNER_BR,
        MaskHandleType.FEATHER_T: RotoHandleType.FEATHER_T,
        MaskHandleType.FEATHER_B: RotoHandleType.FEATHER_B,
        MaskHandleType.FEATHER_L: RotoHandleType.FEATHER_L,
        MaskHandleType.FEATHER_R: RotoHandleType.FEATHER_R,
        MaskHandleType.POLYGON_RADIUS: RotoHandleType.POLYGON_RADIUS,
    }

    for mask_def in mask_defs:
        mask_type = mask_def.handle_type
        if mask_type in type_conversion:
            roto_type = type_conversion[mask_type]
            definitions.append(HandleDefinition(
                handle_type=roto_type,
                ndc_position=mask_def.ndc_position,
                color=mask_def.color,
                radius=mask_def.radius,
                is_source=False,
                cursor_hint=mask_def.cursor_hint,
            ))

    # Add opacity handle if present
    if handles_data.opacity is not None:
        opacity_handle_id = RotoHandleType.OPACITY.value
        color = MASK_COLOR_OPACITY
        if opacity_handle_id == active_handle:
            color = HANDLE_COLOR_ACTIVE
        elif opacity_handle_id == hover_handle:
            color = HANDLE_COLOR_HOVER

        definitions.append(HandleDefinition(
            handle_type=RotoHandleType.OPACITY,
            ndc_position=handles_data.opacity,
            color=color,
            radius=8.0,
            is_source=False,
            cursor_hint="size_ver",
        ))

    return definitions


# ── Mask/Roto Parameter Update Functions (S5-11) ──────────────────────────────

def compute_mask_delta_params(
    handle_id: str,
    drag_start_image_pos: Tuple[float, float],
    drag_current_image_pos: Tuple[float, float],
    original_params: Dict[str, Any],
    image_width: int,
    image_height: int,
    modifiers: int = 0,
) -> Dict[str, float]:
    """Compute new mask parameters based on handle drag.

    Implements S5-11 interaction logic:
    - Center handle: updates center_x, center_y
    - Rotate handle: updates rotation (angle from center)
    - Width handles: updates width parameter
    - Height handles: updates height parameter
    - Corner handles: updates width and height together
    - Feather handles: updates feather parameter
    - Polygon radius: updates width/height proportionally

    Args:
        handle_id: The handle being dragged (e.g., "mask_center", "mask_rotate")
        drag_start_image_pos: Initial position in image pixels
        drag_current_image_pos: Current position in image pixels
        original_params: Original node parameters before drag
        image_width, image_height: Image dimensions
        modifiers: Keyboard modifiers (AUD-11310: a plain corner drag scales
            uniformly — matching the Transform tool's Fusion-style default;
            Shift frees the axes for independent x/y scaling)

    Returns:
        Dict of parameter names to new values.
    """
    dx = drag_current_image_pos[0] - drag_start_image_pos[0]
    dy = drag_current_image_pos[1] - drag_start_image_pos[1]

    # AUD-11306: same enum-vs-int defense as the transform delta function —
    # a raw Qt.KeyboardModifier flag object must not TypeError on the
    # bitwise Shift test in the corner branch below.
    modifiers = _normalize_modifiers(modifiers)

    new_params = {}

    # Get original values
    orig_cx = original_params.get("center_x", 0.5)
    orig_cy = original_params.get("center_y", 0.5)
    orig_w = original_params.get("width", 0.5)
    orig_h = original_params.get("height", 0.5)
    orig_rot = original_params.get("rotation", 0.0)
    orig_feather = original_params.get("feather", 0.0)

    # Convert delta to normalized coordinates
    dx_norm = dx / image_width
    dy_norm = dy / image_height

    handle_type = MaskHandleType(handle_id) if handle_id in [h.value for h in MaskHandleType] else None

    if handle_type is None:
        return new_params

    if handle_type == MaskHandleType.CENTER:
        # Move center
        new_params["center_x"] = orig_cx + dx_norm
        new_params["center_y"] = orig_cy + dy_norm

    elif handle_type == MaskHandleType.ROTATE:
        # AUD-11302: rotation is RELATIVE to the drag start, not an absolute
        # angle from the handle's rest position — the rotate handle stays
        # above the bbox for any rotation, so an absolute formula snapped an
        # already-rotated shape back to ~0° the moment the drag began.
        # AUD-11303: the mouse angle uses the same clockwise-from-up
        # convention as the Transform rotate handle (atan2(dx, -dy) in
        # Y-down image space). Mask handles/rendering are counter-clockwise
        # positive on screen (R(+θ) in Y-up NDC, see compute_mask_handles),
        # so a clockwise mouse delta must DECREASE the stored rotation for
        # the shape to follow the cursor.
        center_px_x = orig_cx * image_width
        center_px_y = orig_cy * image_height

        def _clockwise_from_center(px: float, py: float) -> float:
            rel_x = px - center_px_x
            rel_y = py - center_px_y
            return math.degrees(math.atan2(rel_x, -rel_y))

        start_angle = _clockwise_from_center(
            drag_start_image_pos[0], drag_start_image_pos[1])
        cur_angle = _clockwise_from_center(
            drag_current_image_pos[0], drag_current_image_pos[1])
        new_rotation = _wrap_180(orig_rot - (cur_angle - start_angle))
        new_params["rotation"] = new_rotation

    elif handle_type == MaskHandleType.WIDTH_L:
        # Scale width from left edge (decrease width when moving right)
        orig_half_w_px = (orig_w / 2.0) * image_width
        if orig_half_w_px > 0:
            scale_delta = -dx / orig_half_w_px
            new_w = max(0.01, orig_w + orig_w * scale_delta)
            new_params["width"] = new_w
            # AUD-11300: keep the RIGHT edge fixed. The anchor moves linearly
            # with the size change; the old quadratic-ish formula
            # (dx_norm * (1 - new_w/orig_w) / 2) let the opposite edge drift
            # (right edge slid by 10% of the drag).
            new_params["center_x"] = orig_cx + (orig_w - new_w) / 2.0

    elif handle_type == MaskHandleType.WIDTH_R:
        # Scale width from right edge (increase width when moving right)
        orig_half_w_px = (orig_w / 2.0) * image_width
        if orig_half_w_px > 0:
            scale_delta = dx / orig_half_w_px
            new_w = max(0.01, orig_w + orig_w * scale_delta)
            new_params["width"] = new_w
            # AUD-11300: keep the LEFT edge fixed (linear anchor shift).
            new_params["center_x"] = orig_cx + (new_w - orig_w) / 2.0

    elif handle_type == MaskHandleType.HEIGHT_T:
        # Scale height from top edge (Y-down in image coords)
        orig_half_h_px = (orig_h / 2.0) * image_height
        if orig_half_h_px > 0:
            scale_delta = -dy / orig_half_h_px  # Negative because Y-down
            new_h = max(0.01, orig_h + orig_h * scale_delta)
            new_params["height"] = new_h
            # AUD-11300: keep the BOTTOM edge fixed (linear anchor shift).
            new_params["center_y"] = orig_cy + (orig_h - new_h) / 2.0

    elif handle_type == MaskHandleType.HEIGHT_B:
        # Scale height from bottom edge
        orig_half_h_px = (orig_h / 2.0) * image_height
        if orig_half_h_px > 0:
            scale_delta = dy / orig_half_h_px
            new_h = max(0.01, orig_h + orig_h * scale_delta)
            new_params["height"] = new_h
            # AUD-11300: keep the TOP edge fixed (linear anchor shift).
            new_params["center_y"] = orig_cy + (new_h - orig_h) / 2.0

    elif handle_type in (MaskHandleType.CORNER_TL, MaskHandleType.CORNER_TR,
                         MaskHandleType.CORNER_BL, MaskHandleType.CORNER_BR):
        # Corner scale (dual axis)
        # AUD-11306: modifiers is normalized to an int at function entry, so
        # this bitwise test is safe for Qt enum inputs as well.
        # AUD-11310: unify the modifier contract with the Transform tool
        # (line ~1031): a plain corner drag scales uniformly preserving the
        # original aspect ratio, Shift frees the axes for independent x/y
        # scaling. The old mask convention was the exact opposite, which
        # made the same modifier mean opposite things between the two tools.
        free_axes = (modifiers & _SHIFT_MODIFIER_MASK) != 0

        orig_half_w_px = (orig_w / 2.0) * image_width
        orig_half_h_px = (orig_h / 2.0) * image_height

        if handle_type == MaskHandleType.CORNER_TL:
            # Top-left: both dx and dy negative (moving toward corner)
            if orig_half_w_px > 0:
                w_delta = -dx / orig_half_w_px
                new_params["width"] = max(0.01, orig_w + orig_w * w_delta)
            if orig_half_h_px > 0:
                h_delta = -dy / orig_half_h_px
                new_params["height"] = max(0.01, orig_h + orig_h * h_delta)

        elif handle_type == MaskHandleType.CORNER_TR:
            if orig_half_w_px > 0:
                w_delta = dx / orig_half_w_px
                new_params["width"] = max(0.01, orig_w + orig_w * w_delta)
            if orig_half_h_px > 0:
                h_delta = -dy / orig_half_h_px
                new_params["height"] = max(0.01, orig_h + orig_h * h_delta)

        elif handle_type == MaskHandleType.CORNER_BL:
            if orig_half_w_px > 0:
                w_delta = -dx / orig_half_w_px
                new_params["width"] = max(0.01, orig_w + orig_w * w_delta)
            if orig_half_h_px > 0:
                h_delta = dy / orig_half_h_px
                new_params["height"] = max(0.01, orig_h + orig_h * h_delta)

        elif handle_type == MaskHandleType.CORNER_BR:
            if orig_half_w_px > 0:
                w_delta = dx / orig_half_w_px
                new_params["width"] = max(0.01, orig_w + orig_w * w_delta)
            if orig_half_h_px > 0:
                h_delta = dy / orig_half_h_px
                new_params["height"] = max(0.01, orig_h + orig_h * h_delta)

        # Uniform scale unless Shift freed the axes (AUD-11310)
        if (not free_axes and "width" in new_params
                and "height" in new_params):
            avg_scale = (new_params["width"] / orig_w + new_params["height"] / orig_h) / 2.0
            new_params["width"] = orig_w * avg_scale
            new_params["height"] = orig_h * avg_scale

        # AUD-11300: anchor the OPPOSITE corner (consistent with the edge
        # handles above). The old code scaled around the shape center, so
        # the opposite corner moved with the drag instead of staying put.
        if "width" in new_params:
            new_w = new_params["width"]
            if handle_type in (MaskHandleType.CORNER_TL,
                               MaskHandleType.CORNER_BL):
                # Right side anchored
                new_params["center_x"] = orig_cx + (orig_w - new_w) / 2.0
            else:
                # Left side anchored
                new_params["center_x"] = orig_cx + (new_w - orig_w) / 2.0
        if "height" in new_params:
            new_h = new_params["height"]
            if handle_type in (MaskHandleType.CORNER_TL,
                               MaskHandleType.CORNER_TR):
                # Bottom side anchored (image Y-down)
                new_params["center_y"] = orig_cy + (orig_h - new_h) / 2.0
            else:
                # Top side anchored
                new_params["center_y"] = orig_cy + (new_h - orig_h) / 2.0

    elif handle_type in (MaskHandleType.FEATHER_T, MaskHandleType.FEATHER_B,
                         MaskHandleType.FEATHER_L, MaskHandleType.FEATHER_R):
        # Feather adjustment
        # Feather delta is based on movement perpendicular to edge
        # Convert pixel delta to normalized feather delta
        # Feather range is 0-0.5 for MaskNode

        # AUD-11304: feather handles are drawn OUTSIDE the shape along the
        # outward normals (see compute_mask_handles), so dragging outward
        # must INCREASE feather on all four handles. The old per-handle
        # sign table made T/L fight the mouse (outward drag shrank the
        # feather). Project the drag onto the outward direction instead of
        # hardcoding per-handle signs.
        if handle_type == MaskHandleType.FEATHER_T:
            outward = -dy / image_height  # outward = up (Y-down coords)
        elif handle_type == MaskHandleType.FEATHER_B:
            outward = dy / image_height   # outward = down
        elif handle_type == MaskHandleType.FEATHER_L:
            outward = -dx / image_width   # outward = left
        else:  # MaskHandleType.FEATHER_R
            outward = dx / image_width    # outward = right

        new_feather = max(0.0, min(0.5, orig_feather + outward * 0.5))
        new_params["feather"] = new_feather

    elif handle_type == MaskHandleType.POLYGON_RADIUS:
        # Polygon radius: adjust width/height proportionally
        # Distance from center determines radius
        center_px_x = orig_cx * image_width
        center_px_y = orig_cy * image_height
        dist = math.sqrt(
            (drag_current_image_pos[0] - center_px_x) ** 2 +
            (drag_current_image_pos[1] - center_px_y) ** 2
        )
        # Radius = dist / image_size (normalized)
        radius_norm = dist / max(image_width, image_height)
        # Width and height are both radius_norm * 2 for polygon
        new_params["width"] = radius_norm * 2.0
        new_params["height"] = radius_norm * 2.0

    return new_params


def compute_roto_delta_params(
    handle_id: str,
    drag_start_image_pos: Tuple[float, float],
    drag_current_image_pos: Tuple[float, float],
    original_params: Dict[str, Any],
    image_width: int,
    image_height: int,
    modifiers: int = 0,
) -> Dict[str, float]:
    """Compute new roto parameters based on handle drag.

    Extends Mask delta params with opacity control.

    Args:
        handle_id: The handle being dragged (e.g., "roto_center", "roto_opacity")
        drag_start_image_pos: Initial position in image pixels
        drag_current_image_pos: Current position in image pixels
        original_params: Original node parameters before drag
        image_width, image_height: Image dimensions
        modifiers: Keyboard modifiers

    Returns:
        Dict of parameter names to new values.
    """
    new_params = {}

    # AUD-11306: normalize Qt-enum modifier inputs to an int bitmask before
    # delegating (the mask delta function does the same at its entry).
    modifiers = _normalize_modifiers(modifiers)

    # Check if it's a roto-specific handle
    handle_type = RotoHandleType(handle_id) if handle_id in [h.value for h in RotoHandleType] else None

    if handle_type is None:
        return new_params

    # Handle opacity control (roto-specific)
    if handle_type == RotoHandleType.OPACITY:
        # Opacity is controlled by vertical position (image coords, Y-down).
        # AUD-11305: semantics locked by
        # tests/composite/test_composite_mask_roto_handles.py — moving the
        # handle DOWN decreases opacity, moving UP increases it (the two old
        # inline comments said the opposite of the code; the comments were
        # wrong, not the code).
        orig_opacity = original_params.get("opacity", 1.0)

        # Convert drag delta to opacity change
        dy = drag_current_image_pos[1] - drag_start_image_pos[1]
        opacity_delta = -dy / image_height  # Y-down: dy>0 (down) → decrease

        new_opacity = max(0.0, min(1.0, orig_opacity + opacity_delta * 2.0))
        new_params["opacity"] = new_opacity

        return new_params

    # For other handles, use mask delta params logic
    # Convert RotoHandleType to MaskHandleType
    type_conversion = {
        RotoHandleType.CENTER: MaskHandleType.CENTER,
        RotoHandleType.ROTATE: MaskHandleType.ROTATE,
        RotoHandleType.WIDTH_L: MaskHandleType.WIDTH_L,
        RotoHandleType.WIDTH_R: MaskHandleType.WIDTH_R,
        RotoHandleType.HEIGHT_T: MaskHandleType.HEIGHT_T,
        RotoHandleType.HEIGHT_B: MaskHandleType.HEIGHT_B,
        RotoHandleType.CORNER_TL: MaskHandleType.CORNER_TL,
        RotoHandleType.CORNER_TR: MaskHandleType.CORNER_TR,
        RotoHandleType.CORNER_BL: MaskHandleType.CORNER_BL,
        RotoHandleType.CORNER_BR: MaskHandleType.CORNER_BR,
        RotoHandleType.FEATHER_T: MaskHandleType.FEATHER_T,
        RotoHandleType.FEATHER_B: MaskHandleType.FEATHER_B,
        RotoHandleType.FEATHER_L: MaskHandleType.FEATHER_L,
        RotoHandleType.FEATHER_R: MaskHandleType.FEATHER_R,
        RotoHandleType.POLYGON_RADIUS: MaskHandleType.POLYGON_RADIUS,
    }

    if handle_type in type_conversion:
        mask_handle_id = type_conversion[handle_type].value
        new_params = compute_mask_delta_params(
            mask_handle_id,
            drag_start_image_pos,
            drag_current_image_pos,
            original_params,
            image_width,
            image_height,
            modifiers,
        )

    return new_params


# ── Constant gradient/radial handles ─────────────────────────────────────────
#
# Constant's gradient and radial modes expose on-screen handles for positioning
# the gradient (start/end points or center/radius) and a feather control for the
# transition softness. Coordinates are stored normalized 0-1 in node params and
# converted to NDC for drawing using the same image_ndc_rect convention as the
# mask handles above.

# Cyan-ish color for the gradient transition line (matches mask auxiliary lines).
CONSTANT_COLOR_LINE = (1.0, 1.0, 1.0, 0.4)
# Cyan for feather handle (matches mask feather handle color).
CONSTANT_COLOR_FEATHER = MASK_COLOR_FEATHER  # reused from mask section


class ConstantHandleType(Enum):
    """Handle types for Constant node gradient/radial interaction."""
    GRAD_START = "constant_grad_start"      # gradient start point (color1)
    GRAD_END = "constant_grad_end"          # gradient end point (color2)
    RADIAL_CENTER = "constant_radial_center"  # radial center
    RADIAL_RADIUS = "constant_radial_radius"  # radial radius endpoint
    FEATHER = "constant_feather"            # feather control (mode-dependent)


@dataclass
class ConstantHandlesData:
    """Computed handle positions for a Constant node (gradient/radial mode).

    All positions are in NDC coordinates relative to the current viewport.
    Only the fields relevant to the active mode are meaningful; the rest are
    left at default values.
    """
    # gradient mode
    start: Tuple[float, float] = (0.0, 0.0)
    end: Tuple[float, float] = (0.0, 0.0)
    # radial mode
    center: Tuple[float, float] = (0.0, 0.0)
    radius_edge: Tuple[float, float] = (0.0, 0.0)
    # feather (both modes) — position depends on mode, computed by caller
    feather: Tuple[float, float] = (0.0, 0.0)
    active_mode: str = "gradient"           # "gradient" or "radial"


def _norm_to_ndc(
    nx: float,
    ny: float,
    image_ndc_rect: Tuple[float, float, float, float],
) -> Tuple[float, float]:
    """Convert a normalized (0-1, image-Y-down) point to NDC (Y-up).

    Matches the convention used by compute_mask_handles: image Y grows
    downward, NDC Y grows upward.
    """
    ndc_x0, ndc_y0, ndc_x1, ndc_y1 = image_ndc_rect
    ndc_w = ndc_x1 - ndc_x0
    ndc_h = ndc_y1 - ndc_y0
    return (ndc_x0 + nx * ndc_w, ndc_y1 - ny * ndc_h)


def compute_constant_handles(
    mode: str,
    start_x: float, start_y: float,
    end_x: float, end_y: float,
    center_x: float, center_y: float,
    radius: float,
    feather: float,
    image_width: int,
    image_height: int,
    image_ndc_rect: Tuple[float, float, float, float],
) -> ConstantHandlesData:
    """Compute handle positions for a Constant node's gradient/radial mode.

    Args:
        mode: "gradient" or "radial".
        start_x, start_y, end_x, end_y: Normalized (0-1) gradient endpoints.
        center_x, center_y: Normalized (0-1) radial center.
        radius: Normalized radius (short-edge basis, matches shader).
        feather: Normalized 0-1 softness fraction.
        image_width, image_height: Image pixel dimensions.
        image_ndc_rect: The base image rectangle in NDC (x0, y0, x1, y1).

    Returns:
        ConstantHandlesData with all handle positions in NDC. The feather field
        is unused (feather has no on-screen handle; it's a panel-slider-only
        scalar) and is left at its default.
    """
    if mode == "radial":
        center_ndc = _norm_to_ndc(center_x, center_y, image_ndc_rect)
        # radius is normalized on the short edge; convert to an NDC magnitude.
        # The radius endpoint sits along +X from center in image space.
        short_edge = float(min(image_width, image_height)) if image_width > 0 and image_height > 0 else 1.0
        radius_end_x = center_x + (radius * short_edge / image_width) if image_width > 0 else center_x
        radius_end_y = center_y
        radius_edge_ndc = _norm_to_ndc(radius_end_x, radius_end_y, image_ndc_rect)
        return ConstantHandlesData(
            center=center_ndc,
            radius_edge=radius_edge_ndc,
            active_mode="radial",
        )

    # gradient (default)
    start_ndc = _norm_to_ndc(start_x, start_y, image_ndc_rect)
    end_ndc = _norm_to_ndc(end_x, end_y, image_ndc_rect)
    return ConstantHandlesData(
        start=start_ndc,
        end=end_ndc,
        active_mode="gradient",
    )


def compute_constant_delta_params(
    handle_id: str,
    drag_start_image_pos: Tuple[float, float],
    drag_current_image_pos: Tuple[float, float],
    original_params: Dict[str, Any],
    image_width: int,
    image_height: int,
    modifiers: int = 0,
) -> Dict[str, float]:
    """Compute new Constant parameters based on a handle drag.

    - GRAD_START / GRAD_END / RADIAL_CENTER: map the cursor's image position
      directly to the normalized (0-1) param (image Y-down).
    - RADIAL_RADIUS: derive radius from cursor distance to the center,
      normalized by the short edge.
    - FEATHER: convert the radial drag distance (from the drag start) into a
      feather delta. This is intentionally a relative control — the absolute
      position of the feather handle is a visualization aid, not a 1:1 mapping.

    Args:
        handle_id: The handle being dragged (ConstantHandleType value).
        drag_start_image_pos: Initial position in image pixels.
        drag_current_image_pos: Current position in image pixels.
        original_params: Original node parameters before drag.
        image_width, image_height: Image dimensions.
        modifiers: Keyboard modifiers (unused for now).

    Returns:
        Dict of parameter names to new values.
    """
    new_params: Dict[str, float] = {}
    if image_width <= 0 or image_height <= 0:
        return new_params

    try:
        handle_type = ConstantHandleType(handle_id)
    except ValueError:
        return new_params

    # Normalized cursor position (image Y-down).
    nx = drag_current_image_pos[0] / image_width
    ny = drag_current_image_pos[1] / image_height

    if handle_type == ConstantHandleType.GRAD_START:
        new_params["start_x"] = nx
        new_params["start_y"] = ny
    elif handle_type == ConstantHandleType.GRAD_END:
        new_params["end_x"] = nx
        new_params["end_y"] = ny
    elif handle_type == ConstantHandleType.RADIAL_CENTER:
        new_params["center_x"] = nx
        new_params["center_y"] = ny
    elif handle_type == ConstantHandleType.RADIAL_RADIUS:
        # Radius = distance from center to cursor, normalized on short edge.
        cx_px = original_params.get("center_x", 0.5) * image_width
        cy_px = original_params.get("center_y", 0.5) * image_height
        dx = drag_current_image_pos[0] - cx_px
        dy = drag_current_image_pos[1] - cy_px
        short_edge = float(min(image_width, image_height))
        new_radius = math.hypot(dx, dy) / short_edge
        new_params["radial_radius"] = max(0.001, min(2.0, new_radius))
    # FEATHER handle type has no on-screen control (feather is a panel-slider
    # scalar only); if it somehow reaches here, return empty.

    return new_params
