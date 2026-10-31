"""
Viewer Tool System - Unified tool layer for CompositeViewer.

Provides tool modes, interaction state, and coordinate conversion helpers
for select/pan/transform/mask/roto/crop/cornerpin tools.

This module defines the core concepts specified in:
docs/composite_node_upgrade_plan_2026-05-16.md (S5-A to S5-C)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from PyQt6.QtCore import QPointF


class ViewerToolMode(Enum):
    """Tool modes for the CompositeViewer.

    Each mode defines different mouse interaction behaviors:
    - SELECT: Click to select nodes, drag to move selection bbox
    - PAN: Pan the viewport (default, or middle-click/space+drag)
    - TRANSFORM: Edit transform handles (move, scale, rotate, pivot)
    - CROP: Edit crop boundaries
    - MASK: Edit mask/roto control points
    - ROTO: Edit Bezier roto splines
    - CORNERPIN: Edit corner pin positions
    - ROI: Drag a user region of interest — only the ROI is evaluated in
      preview (B1); areas outside stay dimmed until cleared (Shift+R /
      Ctrl+Shift+R)
    - PAINT: Draw clone/erase/solid strokes on a selected Paint node
      (C2). Alt+click sets the clone source point, [ / ] resize the
      brush.
    """
    SELECT = "select"
    PAN = "pan"
    TRANSFORM = "transform"
    CROP = "crop"
    MASK = "mask"
    ROTO = "roto"
    CORNERPIN = "cornerpin"
    ROI = "roi"
    PAINT = "paint"
    # CP-056: eyedropper — click samples the linear pixel color; the page
    # forwards it to the properties panel's last-touched color parameter.
    COLOR_PICKER = "color_picker"


# Tool mode display names for UI
TOOL_MODE_LABELS = {
    ViewerToolMode.SELECT: "Select",
    ViewerToolMode.PAN: "Pan",
    ViewerToolMode.TRANSFORM: "Transform",
    ViewerToolMode.CROP: "Crop",
    ViewerToolMode.MASK: "Mask",
    ViewerToolMode.ROTO: "Roto",
    ViewerToolMode.CORNERPIN: "Corner Pin",
    ViewerToolMode.ROI: "ROI",
    ViewerToolMode.PAINT: "Paint",
    ViewerToolMode.COLOR_PICKER: "Color Picker",
}

# Default keyboard shortcuts (can be overridden in settings)
TOOL_MODE_SHORTCUTS = {
    ViewerToolMode.SELECT: "S",
    ViewerToolMode.PAN: "H",  # Nuke convention
    ViewerToolMode.TRANSFORM: "T",
    ViewerToolMode.CROP: "C",
    ViewerToolMode.MASK: "M",
    ViewerToolMode.ROTO: "R",
    ViewerToolMode.CORNERPIN: "P",
    # Brush convention (PS/AE); "P" is taken by Corner Pin.
    ViewerToolMode.PAINT: "B",
    # CP-056: eyedropper (I is free — S/H/T/C/M/R/P/B taken)
    ViewerToolMode.COLOR_PICKER: "I",
}


@dataclass
class ViewerInteractionState:
    """State object for tracking viewer tool interactions.

    Captures the current tool mode, selected node, active handle,
    drag positions, and original parameter values for undo.

    Coordinates are stored in different spaces:
    - drag_start_view_pos: Qt widget pixel coordinates
    - drag_start_ndc_pos: OpenGL normalized device coordinates (-1 to 1)
    - drag_start_image_pos: Image pixel coordinates (0 to tex_width/height)
    - drag_start_format_pos: Format/BBox coordinates (origin-aware)

    Attributes:
        tool: Current active tool mode.
        selected_node_id: ID of the node being edited, or None.
        active_handle: Identifier of the active handle (e.g., "top_left", "rotate").
        drag_start_view_pos: Mouse position at drag start in view pixels.
        drag_start_ndc_pos: NDC position at drag start.
        drag_start_image_pos: Image pixel position at drag start.
        drag_start_format_pos: Format space position at drag start.
        original_params: Copy of node parameters before drag, for undo.
        final_params: Copy of node parameters after drag, for undo comparison.
        is_dragging: Whether a drag operation is active.
        drag_current_view_pos: Current mouse position during drag.
        drag_current_image_pos: Current image position during drag.
        modifier_keys: Keyboard modifiers held during interaction (Shift, Ctrl, etc.)
    """
    tool: ViewerToolMode = ViewerToolMode.PAN
    selected_node_id: Optional[str] = None
    active_handle: Optional[str] = None
    drag_start_view_pos: QPointF = field(default_factory=lambda: QPointF())
    drag_start_ndc_pos: QPointF = field(default_factory=lambda: QPointF())
    drag_start_image_pos: QPointF = field(default_factory=lambda: QPointF())
    drag_start_format_pos: QPointF = field(default_factory=lambda: QPointF())
    original_params: dict[str, Any] = field(default_factory=dict)
    final_params: dict[str, Any] = field(default_factory=dict)  # S5-13: For undo comparison
    preview_params: dict[str, Any] = field(default_factory=dict)  # S5-12: Preview params during drag
    is_dragging: bool = False
    drag_current_view_pos: QPointF = field(default_factory=lambda: QPointF())
    drag_current_image_pos: QPointF = field(default_factory=lambda: QPointF())
    modifier_keys: int = 0  # Qt.KeyboardModifier flags

    def reset(self) -> None:
        """Reset interaction state to default (no active drag)."""
        self.selected_node_id = None
        self.active_handle = None
        self.drag_start_view_pos = QPointF()
        self.drag_start_ndc_pos = QPointF()
        self.drag_start_image_pos = QPointF()
        self.drag_start_format_pos = QPointF()
        self.original_params.clear()
        self.final_params.clear()
        # AUD-11400: stale preview deltas from the previous drag leaked into
        # the next interaction's press→first-move window; every consumer
        # (composite_viewport / composite_page) adds preview_params on top of
        # original_params, so it must start empty for each interaction.
        self.preview_params.clear()
        self.is_dragging = False
        self.drag_current_view_pos = QPointF()
        self.drag_current_image_pos = QPointF()
        self.modifier_keys = 0

    def start_drag(
        self,
        tool: ViewerToolMode,
        node_id: Optional[str],
        handle: Optional[str],
        view_pos: QPointF,
        ndc_pos: QPointF,
        image_pos: QPointF,
        format_pos: QPointF,
        original_params: dict[str, Any],
        modifiers: int = 0,
    ) -> None:
        """Initialize a drag operation with all coordinate spaces."""
        self.tool = tool
        self.selected_node_id = node_id
        self.active_handle = handle
        self.drag_start_view_pos = view_pos
        self.drag_start_ndc_pos = ndc_pos
        self.drag_start_image_pos = image_pos
        self.drag_start_format_pos = format_pos
        self.original_params = original_params.copy()
        # AUD-11400: each drag starts from a clean preview — until the first
        # mouse-move writes fresh deltas, consumers must see "no preview".
        self.preview_params.clear()
        self.is_dragging = True
        self.drag_current_view_pos = view_pos
        self.drag_current_image_pos = image_pos
        self.modifier_keys = modifiers

    def update_drag(
        self,
        view_pos: QPointF,
        ndc_pos: Optional[QPointF] = None,
        image_pos: Optional[QPointF] = None,
        format_pos: Optional[QPointF] = None,
        modifiers: int = 0,
    ) -> None:
        """Update current drag position (called on mouse move).

        Args:
            modifiers: Keyboard modifiers held during the move, so page-side
                handlers can vary behavior mid-drag (e.g. CornerPin live
                preview with Shift).
        """
        if not self.is_dragging:
            return
        self.drag_current_view_pos = view_pos
        if ndc_pos is not None:
            # Store NDC if needed for delta calculations
            pass
        if image_pos is not None:
            self.drag_current_image_pos = image_pos
        self.modifier_keys = modifiers

    def set_final_params(self, params: dict[str, Any]) -> None:
        """Set the final params after drag for undo comparison.

        Called by CompositePage when interaction ends to record
        the final node parameters for undo command creation.

        Args:
            params: The final node parameters after the drag operation.
        """
        self.final_params = params.copy()

    def end_drag(self) -> dict[str, Any]:
        """End drag and return original params for undo."""
        self.is_dragging = False
        return self.original_params.copy()

    def get_image_delta(self) -> QPointF:
        """Get the drag delta in image pixel coordinates."""
        return self.drag_current_image_pos - self.drag_start_image_pos

    def get_view_delta(self) -> QPointF:
        """Get the drag delta in view pixel coordinates."""
        return self.drag_current_view_pos - self.drag_start_view_pos


# ── Handle Type Definitions ──────────────────────────────────────────────────

class HandleType(Enum):
    """Types of viewer control handles."""
    # Transform handles
    CENTER = "center"          # Move/translate center point
    TOP_LEFT = "top_left"      # Scale corner
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP_EDGE = "top_edge"      # Scale edge
    BOTTOM_EDGE = "bottom_edge"
    LEFT_EDGE = "left_edge"
    RIGHT_EDGE = "right_edge"
    ROTATE = "rotate"          # Rotation handle (usually outside bbox)
    PIVOT = "pivot"            # Pivot/anchor point

    # Crop handles (edge and corner)
    CROP_LEFT = "crop_left"
    CROP_RIGHT = "crop_right"
    CROP_TOP = "crop_top"
    CROP_BOTTOM = "crop_bottom"
    CROP_TOP_LEFT = "crop_top_left"
    CROP_TOP_RIGHT = "crop_top_right"
    CROP_BOTTOM_LEFT = "crop_bottom_left"
    CROP_BOTTOM_RIGHT = "crop_bottom_right"
    CROP_CENTER = "crop_center"  # Center point for moving entire crop region

    # Reformat handles (format boundary corners/edges)
    REFORMAT_LEFT = "reformat_left"
    REFORMAT_RIGHT = "reformat_right"
    REFORMAT_TOP = "reformat_top"
    REFORMAT_BOTTOM = "reformat_bottom"
    REFORMAT_TOP_LEFT = "reformat_top_left"
    REFORMAT_TOP_RIGHT = "reformat_top_right"
    REFORMAT_BOTTOM_LEFT = "reformat_bottom_left"
    REFORMAT_BOTTOM_RIGHT = "reformat_bottom_right"
    REFORMAT_CENTER = "reformat_center"

    # CornerPin handles
    CORNERPIN_0 = "cornerpin_0"  # Top-left
    CORNERPIN_1 = "cornerpin_1"  # Top-right
    CORNERPIN_2 = "cornerpin_2"  # Bottom-right
    CORNERPIN_3 = "cornerpin_3"  # Bottom-left

    # Mask handles (S5-11: geometric mask control points)
    # Center and rotate
    MASK_CENTER = "mask_center"           # Center crosshair (translate)
    MASK_ROTATE = "mask_rotate"           # Rotation handle (cyan, outside bbox)
    # Width/height edge handles
    MASK_WIDTH_L = "mask_width_l"         # Left edge (width control)
    MASK_WIDTH_R = "mask_width_r"         # Right edge (width control)
    MASK_HEIGHT_T = "mask_height_t"       # Top edge (height control)
    MASK_HEIGHT_B = "mask_height_b"       # Bottom edge (height control)
    # Corner handles
    MASK_CORNER_TL = "mask_corner_tl"     # Top-left corner
    MASK_CORNER_TR = "mask_corner_tr"     # Top-right corner
    MASK_CORNER_BL = "mask_corner_bl"     # Bottom-left corner
    MASK_CORNER_BR = "mask_corner_br"     # Bottom-right corner
    # Feather handles (cyan)
    MASK_FEATHER_T = "mask_feather_t"     # Top feather edge
    MASK_FEATHER_B = "mask_feather_b"     # Bottom feather edge
    MASK_FEATHER_L = "mask_feather_l"     # Left feather edge
    MASK_FEATHER_R = "mask_feather_r"     # Right feather edge
    # Polygon specific
    MASK_POLYGON_RADIUS = "mask_polygon_radius"  # Polygon radius control
    # Legacy aliases (for backward compatibility)
    MASK_POINT = "mask_point"             # Alias for corner handles
    MASK_FEATHER = "mask_feather"         # Alias for feather handles
    # Bezier tangent handles (for future custom shapes)
    MASK_TANGENT_IN = "mask_tangent_in"   # Bezier tangent in
    MASK_TANGENT_OUT = "mask_tangent_out"  # Bezier tangent out

    # Roto handles (S5-11: extends Mask with opacity control)
    # All mask handles + roto-specific
    ROTO_CENTER = "roto_center"
    ROTO_ROTATE = "roto_rotate"
    ROTO_WIDTH_L = "roto_width_l"
    ROTO_WIDTH_R = "roto_width_r"
    ROTO_HEIGHT_T = "roto_height_t"
    ROTO_HEIGHT_B = "roto_height_b"
    ROTO_CORNER_TL = "roto_corner_tl"
    ROTO_CORNER_TR = "roto_corner_tr"
    ROTO_CORNER_BL = "roto_corner_bl"
    ROTO_CORNER_BR = "roto_corner_br"
    ROTO_FEATHER_T = "roto_feather_t"
    ROTO_FEATHER_B = "roto_feather_b"
    ROTO_FEATHER_L = "roto_feather_l"
    ROTO_FEATHER_R = "roto_feather_r"
    ROTO_POLYGON_RADIUS = "roto_polygon_radius"
    ROTO_OPACITY = "roto_opacity"         # Opacity control (yellow tint)

    # Selection handles
    SELECTION_BOX = "selection_box"  # Bbox for selection


# ── Coordinate Conversion Helpers ─────────────────────────────────────────────

def view_pos_to_ndc(
    view_pos: QPointF,
    viewport_width: float,
    viewport_height: float,
    device_pixel_ratio: float = 1.0,
) -> QPointF:
    """Convert Qt widget pixel coordinates to OpenGL NDC (-1 to 1).

    Args:
        view_pos: Position in Qt widget pixels (top-left origin).
        viewport_width: Widget width in pixels.
        viewport_height: Widget height in pixels.
        device_pixel_ratio: Screen DPR (default 1.0).

    Returns:
        QPointF in NDC space (-1 to 1), Y flipped (up is positive).
    """
    vp_w = viewport_width * device_pixel_ratio
    vp_h = viewport_height * device_pixel_ratio

    mx = view_pos.x() * device_pixel_ratio
    my = view_pos.y() * device_pixel_ratio

    ndc_x = (mx / (vp_w / 2.0)) - 1.0
    ndc_y = 1.0 - (my / (vp_h / 2.0))  # Flip Y

    return QPointF(ndc_x, ndc_y)


def ndc_to_view_pos(
    ndc_pos: QPointF,
    viewport_width: float,
    viewport_height: float,
    device_pixel_ratio: float = 1.0,
) -> QPointF:
    """Convert OpenGL NDC to Qt widget pixel coordinates.

    Args:
        ndc_pos: Position in NDC space (-1 to 1).
        viewport_width: Widget width in pixels.
        viewport_height: Widget height in pixels.
        device_pixel_ratio: Screen DPR (default 1.0).

    Returns:
        QPointF in Qt widget pixels (top-left origin).
    """
    vp_w = viewport_width * device_pixel_ratio
    vp_h = viewport_height * device_pixel_ratio

    px_x = (ndc_pos.x() + 1.0) * (vp_w / 2.0) / device_pixel_ratio
    px_y = (1.0 - ndc_pos.y()) * (vp_h / 2.0) / device_pixel_ratio

    return QPointF(px_x, px_y)


def ndc_to_image_pos(
    ndc_pos: QPointF,
    image_ndc_rect: tuple[float, float, float, float],  # x0, y0, x1, y1
    image_width: float,
    image_height: float,
    check_bounds: bool = True,
) -> Optional[QPointF]:
    """Convert NDC coordinates to image pixel coordinates.

    Args:
        ndc_pos: Position in NDC space.
        image_ndc_rect: The image rectangle in NDC (x0, y0, x1, y1)
                        computed with zoom/pan applied.
        image_width: Image width in pixels.
        image_height: Image height in pixels.
        check_bounds: If True, check if the point is inside the image bounds.

    Returns:
        QPointF in image pixels (top-left origin), or None if outside image (when check_bounds is True).
    """
    x0, y0, x1, y1 = image_ndc_rect

    if x1 == x0 or y1 == y0:
        return None

    # Map NDC to UV (0 to 1)
    u = (ndc_pos.x() - x0) / (x1 - x0)
    v = (ndc_pos.y() - y0) / (y1 - y0)

    # Check bounds
    if check_bounds:
        if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
            return None

    # UV to image pixels (Y flip: NDC Y-up, image Y-down)
    px_x = u * image_width
    px_y = (1.0 - v) * image_height

    return QPointF(px_x, px_y)


def image_pos_to_ndc(
    image_pos: QPointF,
    image_ndc_rect: tuple[float, float, float, float],
    image_width: float,
    image_height: float,
) -> QPointF:
    """Convert image pixel coordinates to NDC.

    Args:
        image_pos: Position in image pixels (top-left origin).
        image_ndc_rect: The image rectangle in NDC (x0, y0, x1, y1).
        image_width: Image width in pixels.
        image_height: Image height in pixels.

    Returns:
        QPointF in NDC space.
    """
    x0, y0, x1, y1 = image_ndc_rect

    # Image to UV (Y flip)
    u = image_pos.x() / image_width
    v = 1.0 - (image_pos.y() / image_height)

    # UV to NDC
    ndc_x = x0 + (x1 - x0) * u
    ndc_y = y0 + (y1 - y0) * v

    return QPointF(ndc_x, ndc_y)


def image_pos_to_format_pos(
    image_pos: QPointF,
    format_origin_x: float,
    format_origin_y: float,
    format_width: float,
    format_height: float,
) -> QPointF:
    """Convert image pixel coordinates to format/BBox coordinates.

    Format coordinates account for the origin offset of the working space.
    In many compositing applications, the format origin can be non-zero
    (e.g., centered at 0,0 or offset to a specific corner).

    Args:
        image_pos: Position in image pixels (top-left origin relative to bbox).
        format_origin_x: X origin of the format in format units.
        format_origin_y: Y origin of the format in format units.
        format_width: Format width in pixels.
        format_height: Format height in pixels.

    Returns:
        QPointF in format coordinates.
    """
    # Image pixels to format-relative pixels
    fmt_x = image_pos.x() + format_origin_x
    fmt_y = image_pos.y() + format_origin_y

    return QPointF(fmt_x, fmt_y)


def format_pos_to_image_pos(
    format_pos: QPointF,
    format_origin_x: float,
    format_origin_y: float,
) -> QPointF:
    """Convert format coordinates to image pixel coordinates.

    Args:
        format_pos: Position in format coordinates.
        format_origin_x: X origin of the format.
        format_origin_y: Y origin of the format.

    Returns:
        QPointF in image pixels (top-left origin).
    """
    img_x = format_pos.x() - format_origin_x
    img_y = format_pos.y() - format_origin_y

    return QPointF(img_x, img_y)


def compute_image_ndc_rect(
    texture_width: float,
    texture_height: float,
    viewport_width: float,
    viewport_height: float,
    zoom_level: float,
    pan_x: float,
    pan_y: float,
    device_pixel_ratio: float = 1.0,
) -> tuple[float, float, float, float]:
    """Compute the image rectangle in NDC coordinates.

    This accounts for letterbox, zoom, and pan to produce the exact
    NDC bounds where the image is drawn.

    Args:
        texture_width: Texture width in pixels.
        texture_height: Texture height in pixels.
        viewport_width: Widget width in pixels.
        viewport_height: Widget height in pixels.
        zoom_level: Current zoom (1.0 = fit-to-window).
        pan_x: Pan offset in NDC.
        pan_y: Pan offset in NDC.
        device_pixel_ratio: Screen DPR.

    Returns:
        Tuple (x0, y0, x1, y1) of NDC coordinates.
    """
    vp_w = viewport_width * device_pixel_ratio
    vp_h = viewport_height * device_pixel_ratio

    if texture_width <= 0 or texture_height <= 0 or vp_w <= 0 or vp_h <= 0:
        return (-1.0, -1.0, 1.0, 1.0)

    tex_aspect = texture_width / texture_height
    vp_aspect = vp_w / vp_h

    # Base letterbox rect (fit-to-window, centered)
    if tex_aspect > vp_aspect:
        sy = vp_aspect / tex_aspect
        bx0, bx1 = -1.0, 1.0
        by0, by1 = -sy, sy
    else:
        sx = tex_aspect / vp_aspect
        bx0, bx1 = -sx, sx
        by0, by1 = -1.0, 1.0

    # Apply zoom (scale around center)
    cx = (bx0 + bx1) / 2.0
    cy = (by0 + by1) / 2.0
    z = zoom_level
    x0 = cx + (bx0 - cx) * z
    x1 = cx + (bx1 - cx) * z
    y0 = cy + (by0 - cy) * z
    y1 = cy + (by1 - cy) * z

    # Apply pan
    x0 += pan_x
    x1 += pan_x
    y0 += pan_y
    y1 += pan_y

    return (x0, y0, x1, y1)


# ── Hit Testing Helpers ──────────────────────────────────────────────────────

def hit_test_handle(
    view_pos: QPointF,
    handle_view_pos: QPointF,
    hit_radius_pixels: float = 10.0,
) -> bool:
    """Test if a view position hits a handle.

    Uses screen-pixel distance, so hit area is constant regardless of zoom.

    Args:
        view_pos: Current mouse position in view pixels.
        handle_view_pos: Handle position in view pixels.
        hit_radius_pixels: Hit radius in screen pixels (default 10).

    Returns:
        True if within hit radius.
    """
    dx = view_pos.x() - handle_view_pos.x()
    dy = view_pos.y() - handle_view_pos.y()
    dist_sq = dx * dx + dy * dy
    return dist_sq <= (hit_radius_pixels * hit_radius_pixels)


def hit_test_rect(
    view_pos: QPointF,
    rect_view: tuple[float, float, float, float],  # x0, y0, x1, y1 in view pixels
) -> bool:
    """Test if a view position is inside a rectangle.

    Args:
        view_pos: Current mouse position in view pixels.
        rect_view: Rectangle in view pixels (x0, y0, x1, y1).

    Returns:
        True if position is inside rectangle.
    """
    x0, y0, x1, y1 = rect_view
    return (view_pos.x() >= x0 and view_pos.x() <= x1 and
            view_pos.y() >= y0 and view_pos.y() <= y1)


# ── Node Output Region Hit Testing (S5-10) ─────────────────────────────────────

@dataclass
class NodeOutputRegion:
    """Represents a node's output region in the viewer.

    This is used for hit testing when clicking to select nodes in the viewer.

    Attributes:
        node_id: The unique identifier of the node.
        node_type: The node type string (e.g., 'merge', 'transform').
        bbox: The output bounding box in image pixel coordinates (x, y, width, height).
              Uses top-left origin (y=0 at top).
        label: Optional display label for the node.
        z_order: Layer order for hit testing priority (higher = on top).
    """
    node_id: str
    node_type: str
    bbox: tuple[float, float, float, float]  # x, y, width, height in image pixels
    label: Optional[str] = None
    z_order: int = 0


def hit_test_node_output(
    image_pos: QPointF,
    node_regions: list[NodeOutputRegion],
    prefer_top: bool = True,
) -> Optional[NodeOutputRegion]:
    """Test which node output region contains the given image position.

    This is used when clicking in the viewer to select nodes based on their
    output bounding boxes.

    Args:
        image_pos: Position in image pixel coordinates (top-left origin).
        node_regions: List of NodeOutputRegion to test against.
        prefer_top: If True, return the highest z_order region that contains
                    the point. If False, return the first matching region.

    Returns:
        The NodeOutputRegion containing the position, or None if no match.
    """
    matching_regions: list[NodeOutputRegion] = []

    for region in node_regions:
        x, y, w, h = region.bbox
        # Check if point is inside bbox (top-left origin)
        if (image_pos.x() >= x and image_pos.x() < x + w and
            image_pos.y() >= y and image_pos.y() < y + h):
            matching_regions.append(region)

    if not matching_regions:
        return None

    if prefer_top:
        # Return the highest z_order region
        return max(matching_regions, key=lambda r: r.z_order)
    else:
        return matching_regions[0]


def hit_test_handles_in_rect(
    rect_view: tuple[float, float, float, float],  # x0, y0, x1, y1 in view pixels
    handles: dict[str, QPointF],
    hit_radius_pixels: float = 10.0,
) -> list[str]:
    """Find all handles that are inside a selection rectangle.

    Used for rubber band selection (框选) of multiple handles.

    Args:
        rect_view: Selection rectangle in view pixels (x0, y0, x1, y1).
        handles: Dict mapping handle identifiers to view positions.
        hit_radius_pixels: Optional extra hit radius around handles.

    Returns:
        List of handle identifiers that are inside the selection rectangle.
    """
    x0, y0, x1, y1 = rect_view

    # Normalize rect (ensure x0 <= x1, y0 <= y1)
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0

    selected_handles: list[str] = []

    for handle_id, handle_pos in handles.items():
        hx, hy = handle_pos.x(), handle_pos.y()

        # Check if handle is inside rect (with optional radius expansion)
        inside = (hx >= x0 - hit_radius_pixels and
                  hx <= x1 + hit_radius_pixels and
                  hy >= y0 - hit_radius_pixels and
                  hy <= y1 + hit_radius_pixels)

        if inside:
            selected_handles.append(handle_id)

    return selected_handles


# ── Selection State (S5-10) ───────────────────────────────────────────────────

@dataclass
class ViewerSelectionState:
    """Tracks the current selection state in the viewer.

    Attributes:
        selected_node_ids: Set of currently selected node IDs.
        selected_handles: Set of currently selected handle identifiers.
        hovered_node_id: Node ID currently under mouse hover (for highlight).
        hovered_handle: Handle identifier currently under mouse hover.
        rubber_band_active: Whether a rubber band selection is in progress.
        rubber_band_start: Start position of rubber band in view pixels.
        rubber_band_end: End position of rubber band in view pixels.
    """
    selected_node_ids: set[str] = field(default_factory=set)
    selected_handles: set[str] = field(default_factory=set)
    hovered_node_id: Optional[str] = None
    hovered_handle: Optional[str] = None
    rubber_band_active: bool = False
    rubber_band_start: QPointF = field(default_factory=lambda: QPointF())
    rubber_band_end: QPointF = field(default_factory=lambda: QPointF())

    def clear_selection(self) -> None:
        """Clear all selections."""
        self.selected_node_ids.clear()
        self.selected_handles.clear()

    def start_rubber_band(self, start_pos: QPointF) -> None:
        """Start a rubber band selection."""
        self.rubber_band_active = True
        self.rubber_band_start = start_pos
        self.rubber_band_end = start_pos

    def update_rubber_band(self, end_pos: QPointF) -> None:
        """Update rubber band end position."""
        self.rubber_band_end = end_pos

    def end_rubber_band(self) -> tuple[float, float, float, float]:
        """End rubber band and return the selection rect (x0, y0, x1, y1)."""
        self.rubber_band_active = False
        x0, y0 = self.rubber_band_start.x(), self.rubber_band_start.y()
        x1, y1 = self.rubber_band_end.x(), self.rubber_band_end.y()
        # Normalize (ensure x0 <= x1, y0 <= y1)
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        return (x0, y0, x1, y1)

    def add_to_selection(self, node_id: Optional[str] = None,
                         handle_id: Optional[str] = None) -> None:
        """Add a node or handle to the selection."""
        if node_id is not None:
            self.selected_node_ids.add(node_id)
        if handle_id is not None:
            self.selected_handles.add(handle_id)

    def remove_from_selection(self, node_id: Optional[str] = None,
                              handle_id: Optional[str] = None) -> None:
        """Remove a node or handle from the selection."""
        if node_id is not None:
            self.selected_node_ids.discard(node_id)
        if handle_id is not None:
            self.selected_handles.discard(handle_id)

    def toggle_selection(self, node_id: Optional[str] = None,
                         handle_id: Optional[str] = None) -> None:
        """Toggle selection state of a node or handle."""
        if node_id is not None:
            if node_id in self.selected_node_ids:
                self.selected_node_ids.discard(node_id)
            else:
                self.selected_node_ids.add(node_id)
        if handle_id is not None:
            if handle_id in self.selected_handles:
                self.selected_handles.discard(handle_id)
            else:
                self.selected_handles.add(handle_id)


# ── Handle Computation Functions (S5-12) ─────────────────────────────────────

def compute_crop_handles(
    params: dict[str, Any],
    tex_width: float,
    tex_height: float,
    ndc_rect: tuple[float, float, float, float],
    viewport_width: float,
    viewport_height: float,
    dpr: float = 1.0,
) -> dict[str, QPointF]:
    """Compute Crop node handle positions in view coordinates.

    Args:
        params: Crop node parameters (x, y, width, height).
        tex_width: Texture width in pixels.
        tex_height: Texture height in pixels.
        ndc_rect: Current image NDC rect (x0, y0, x1, y1).
        viewport_width: Viewport width in pixels.
        viewport_height: Viewport height in pixels.
        dpr: Device pixel ratio.

    Returns:
        Dict mapping handle IDs to view pixel positions.
    """
    crop_x = int(params.get("x", 0))
    crop_y = int(params.get("y", 0))
    crop_w = int(params.get("width", 512))
    crop_h = int(params.get("height", 512))

    handles = {}

    # Helper: image pos to view pos
    def img_to_view(img_x: float, img_y: float) -> QPointF:
        ndc_pos = image_pos_to_ndc(
            QPointF(img_x, img_y), ndc_rect, tex_width, tex_height
        )
        return ndc_to_view_pos(ndc_pos, viewport_width, viewport_height, dpr)

    # Edge handles (center of each edge)
    handles["crop_top"] = img_to_view(crop_x + crop_w / 2.0, crop_y)
    handles["crop_bottom"] = img_to_view(crop_x + crop_w / 2.0, crop_y + crop_h)
    handles["crop_left"] = img_to_view(crop_x, crop_y + crop_h / 2.0)
    handles["crop_right"] = img_to_view(crop_x + crop_w, crop_y + crop_h / 2.0)

    # Corner handles
    handles["crop_top_left"] = img_to_view(crop_x, crop_y)
    handles["crop_top_right"] = img_to_view(crop_x + crop_w, crop_y)
    handles["crop_bottom_left"] = img_to_view(crop_x, crop_y + crop_h)
    handles["crop_bottom_right"] = img_to_view(crop_x + crop_w, crop_y + crop_h)

    # Center handle
    handles["crop_center"] = img_to_view(crop_x + crop_w / 2.0, crop_y + crop_h / 2.0)

    return handles


def compute_reformat_handles(
    params: dict[str, Any],
    tex_width: float,
    tex_height: float,
    ndc_rect: tuple[float, float, float, float],
    viewport_width: float,
    viewport_height: float,
    dpr: float = 1.0,
) -> dict[str, QPointF]:
    """Compute Reformat node handle positions in view coordinates.

    Args:
        params: Reformat node parameters (width, height, format_mode, preset).
        tex_width: Texture width in pixels.
        tex_height: Texture height in pixels.
        ndc_rect: Current image NDC rect (x0, y0, x1, y1).
        viewport_width: Viewport width in pixels.
        viewport_height: Viewport height in pixels.
        dpr: Device pixel ratio.

    Returns:
        Dict mapping handle IDs to view pixel positions.
    """
    fmt_mode = params.get("format_mode", "preset")
    if fmt_mode == "preset":
        preset_name = params.get("preset", "HD 1080")
        width, height = parse_preset_dimensions(preset_name)
    else:
        width = int(params.get("width", 1920))
        height = int(params.get("height", 1080))

    handles = {}

    def img_to_view(img_x: float, img_y: float) -> QPointF:
        ndc_pos = image_pos_to_ndc(
            QPointF(img_x, img_y), ndc_rect, tex_width, tex_height
        )
        return ndc_to_view_pos(ndc_pos, viewport_width, viewport_height, dpr)

    # Edge handles
    handles["reformat_top"] = img_to_view(width / 2.0, 0)
    handles["reformat_bottom"] = img_to_view(width / 2.0, height)
    handles["reformat_left"] = img_to_view(0, height / 2.0)
    handles["reformat_right"] = img_to_view(width, height / 2.0)

    # Corner handles
    handles["reformat_top_left"] = img_to_view(0, 0)
    handles["reformat_top_right"] = img_to_view(width, 0)
    handles["reformat_bottom_left"] = img_to_view(0, height)
    handles["reformat_bottom_right"] = img_to_view(width, height)

    # Center handle
    handles["reformat_center"] = img_to_view(width / 2.0, height / 2.0)

    return handles


def parse_preset_dimensions(preset_name: str) -> tuple[int, int]:
    """Parse dimensions from preset name."""
    presets = {
        "HD 720": (1280, 720),
        "HD 1080": (1920, 1080),
        "UHD 4K": (3840, 2160),
        "UHD 8K": (7680, 4320),
        "Square 1K": (1024, 1024),
        "Square 2K": (2048, 2048),
        "2K Scope": (2048, 858),
        "4K Scope": (4096, 1716),
    }
    return presets.get(preset_name, (1920, 1080))


def compute_crop_rect_from_handles(
    handle_id: str,
    original_crop: dict[str, int],
    delta_image: QPointF,
    min_size: int = 1,
) -> dict[str, int]:
    """Compute new crop parameters from a handle drag.

    Args:
        handle_id: The handle being dragged.
        original_crop: Original crop params (x, y, width, height).
        delta_image: Drag delta in image pixels.
        min_size: Minimum crop size (default 1).

    Returns:
        New crop parameters dict.
    """
    x = original_crop.get("x", 0)
    y = original_crop.get("y", 0)
    w = original_crop.get("width", 512)
    h = original_crop.get("height", 512)

    dx = int(delta_image.x())
    dy = int(delta_image.y())

    if handle_id == "crop_left":
        new_x = x + dx
        new_w = w - dx
        new_y = y
        new_h = h
    elif handle_id == "crop_right":
        new_x = x
        new_w = w + dx
        new_y = y
        new_h = h
    elif handle_id == "crop_top":
        new_x = x
        new_w = w
        new_y = y + dy
        new_h = h - dy
    elif handle_id == "crop_bottom":
        new_x = x
        new_w = w
        new_y = y
        new_h = h + dy
    elif handle_id == "crop_top_left":
        new_x = x + dx
        new_w = w - dx
        new_y = y + dy
        new_h = h - dy
    elif handle_id == "crop_top_right":
        new_x = x
        new_w = w + dx
        new_y = y + dy
        new_h = h - dy
    elif handle_id == "crop_bottom_left":
        new_x = x + dx
        new_w = w - dx
        new_y = y
        new_h = h + dy
    elif handle_id == "crop_bottom_right":
        new_x = x
        new_w = w + dx
        new_y = y
        new_h = h + dy
    elif handle_id == "crop_center":
        new_x = x + dx
        new_y = y + dy
        new_w = w
        new_h = h
    else:
        return original_crop.copy()

    # Clamp to minimum size
    new_w = max(min_size, new_w)
    new_h = max(min_size, new_h)
    new_x = max(0, new_x)
    new_y = max(0, new_y)

    return {"x": new_x, "y": new_y, "width": new_w, "height": new_h}


def compute_reformat_rect_from_handles(
    handle_id: str,
    original_reformat: dict[str, Any],
    delta_image: QPointF,
    min_size: int = 1,
) -> dict[str, Any]:
    """Compute new reformat parameters from a handle drag.

    Args:
        handle_id: The handle being dragged.
        original_reformat: Original reformat params (width, height, etc).
        delta_image: Drag delta in image pixels.
        min_size: Minimum size (default 1).

    Returns:
        New reformat parameters dict.
    """
    # Get original dimensions
    fmt_mode = original_reformat.get("format_mode", "preset")
    if fmt_mode == "preset":
        preset_name = original_reformat.get("preset", "HD 1080")
        w, h = parse_preset_dimensions(preset_name)
    else:
        w = int(original_reformat.get("width", 1920))
        h = int(original_reformat.get("height", 1080))

    dx = int(delta_image.x())
    dy = int(delta_image.y())

    # Handle drag to resize (similar to crop but anchored at center for some)
    if handle_id in ("reformat_left", "reformat_right"):
        if handle_id == "reformat_left":
            new_w = w - dx
        else:
            new_w = w + dx
        new_h = h
    elif handle_id in ("reformat_top", "reformat_bottom"):
        new_w = w
        if handle_id == "reformat_top":
            new_h = h - dy
        else:
            new_h = h + dy
    elif handle_id == "reformat_top_left":
        new_w = w - dx
        new_h = h - dy
    elif handle_id == "reformat_top_right":
        new_w = w + dx
        new_h = h - dy
    elif handle_id == "reformat_bottom_left":
        new_w = w - dx
        new_h = h + dy
    elif handle_id == "reformat_bottom_right":
        new_w = w + dx
        new_h = h + dy
    else:
        return original_reformat.copy()

    # Clamp to minimum size
    new_w = max(min_size, new_w)
    new_h = max(min_size, new_h)

    # Update params - force custom mode when dragged
    result = original_reformat.copy()
    result["format_mode"] = "custom"
    result["width"] = new_w
    result["height"] = new_h

    return result


# ── Handle Cursor Mapping ───────────────────────────────────────────────────

HANDLE_CURSOR_MAP = {
    # Crop cursors
    "crop_left": "size_hor",
    "crop_right": "size_hor",
    "crop_top": "size_ver",
    "crop_bottom": "size_ver",
    "crop_top_left": "size_fdiag",
    "crop_top_right": "size_bdiag",
    "crop_bottom_left": "size_bdiag",
    "crop_bottom_right": "size_fdiag",
    "crop_center": "size_all",

    # Reformat cursors (same as crop)
    "reformat_left": "size_hor",
    "reformat_right": "size_hor",
    "reformat_top": "size_ver",
    "reformat_bottom": "size_ver",
    "reformat_top_left": "size_fdiag",
    "reformat_top_right": "size_bdiag",
    "reformat_bottom_left": "size_bdiag",
    "reformat_bottom_right": "size_fdiag",
    "reformat_center": "size_all",

    # Transform cursors
    "top_left": "size_fdiag",
    "top_right": "size_bdiag",
    "bottom_left": "size_bdiag",
    "bottom_right": "size_fdiag",
    "top_edge": "size_ver",
    "bottom_edge": "size_ver",
    "left_edge": "size_hor",
    "right_edge": "size_hor",
    "center": "size_all",
    "rotate": "cross",
    "pivot": "cross",
}


def get_cursor_for_handle(handle_id: str) -> str:
    """Get the cursor shape name for a handle.

    Args:
        handle_id: Handle identifier string.

    Returns:
        Cursor shape name (e.g., "size_hor", "size_ver", "size_all").
    """
    return HANDLE_CURSOR_MAP.get(handle_id, "arrow")


# ── Snapping System (S5-14) ──────────────────────────────────────────────────

@dataclass
class SnapConfig:
    """Configuration for snapping behavior.

    Attributes:
        snap_to_format_edge: Enable snapping to format/canvas edges.
        snap_to_format_center: Enable snapping to format/canvas center.
        snap_to_other_bbox: Enable snapping to other node bboxes (optional).
        threshold_pixels: Snapping threshold in screen pixels (default 10).
        threshold_image: Snapping threshold in image pixels (default 5).
        show_snap_lines: Whether to display snap guide lines (default True).
    """
    snap_to_format_edge: bool = True
    snap_to_format_center: bool = True
    snap_to_other_bbox: bool = False
    threshold_pixels: float = 10.0  # Screen pixel threshold
    threshold_image: float = 5.0    # Image pixel threshold
    show_snap_lines: bool = True


@dataclass
class SnapResult:
    """Result of a snapping operation.

    Attributes:
        snapped_position: The snapped position (may be same as input if no snap).
        snapped_x: Whether X coordinate was snapped.
        snapped_y: Whether Y coordinate was snapped.
        snap_target_x: What was snapped to on X (e.g., "format_edge_left", "format_center").
        snap_target_y: What was snapped to on Y (e.g., "format_edge_top", "format_center").
        snap_line_x: Position of snap guide line on X (for visual display), None if not snapped.
        snap_line_y: Position of snap guide line on Y (for visual display), None if not snapped.
    """
    snapped_position: QPointF
    snapped_x: bool = False
    snapped_y: bool = False
    snap_target_x: Optional[str] = None
    snap_target_y: Optional[str] = None
    snap_line_x: Optional[float] = None  # In image pixel coordinates
    snap_line_y: Optional[float] = None  # In image pixel coordinates

    def is_snapped(self) -> bool:
        """Return True if any axis was snapped."""
        return self.snapped_x or self.snapped_y


# Default snap configuration
DEFAULT_SNAP_CONFIG = SnapConfig()


def snap_to_format_edge(
    pos: QPointF,
    format_width: float,
    format_height: float,
    threshold: float = 5.0,
) -> SnapResult:
    """Snap position to format (canvas) edges.

    Format edges are at:
    - Left: x = 0
    - Right: x = format_width
    - Top: y = 0
    - Bottom: y = format_height

    Args:
        pos: Position to snap (in image/format pixel coordinates, top-left origin).
        format_width: Format width in pixels.
        format_height: Format height in pixels.
        threshold: Snapping threshold in pixels.

    Returns:
        SnapResult with snapped position and metadata.
    """
    result = SnapResult(snapped_position=QPointF(pos.x(), pos.y()))

    # Check left edge (x = 0)
    if abs(pos.x() - 0.0) <= threshold:
        result.snapped_position.setX(0.0)
        result.snapped_x = True
        result.snap_target_x = "format_edge_left"
        result.snap_line_x = 0.0

    # Check right edge (x = format_width)
    elif abs(pos.x() - format_width) <= threshold:
        result.snapped_position.setX(format_width)
        result.snapped_x = True
        result.snap_target_x = "format_edge_right"
        result.snap_line_x = format_width

    # Check top edge (y = 0)
    if abs(pos.y() - 0.0) <= threshold:
        result.snapped_position.setY(0.0)
        result.snapped_y = True
        result.snap_target_y = "format_edge_top"
        result.snap_line_y = 0.0

    # Check bottom edge (y = format_height)
    elif abs(pos.y() - format_height) <= threshold:
        result.snapped_position.setY(format_height)
        result.snapped_y = True
        result.snap_target_y = "format_edge_bottom"
        result.snap_line_y = format_height

    return result


def snap_to_format_center(
    pos: QPointF,
    format_width: float,
    format_height: float,
    threshold: float = 5.0,
) -> SnapResult:
    """Snap position to format (canvas) center.

    Format center is at (format_width / 2, format_height / 2).

    Args:
        pos: Position to snap (in image/format pixel coordinates, top-left origin).
        format_width: Format width in pixels.
        format_height: Format height in pixels.
        threshold: Snapping threshold in pixels.

    Returns:
        SnapResult with snapped position and metadata.
    """
    center_x = format_width / 2.0
    center_y = format_height / 2.0

    result = SnapResult(snapped_position=QPointF(pos.x(), pos.y()))

    # Check center X
    if abs(pos.x() - center_x) <= threshold:
        result.snapped_position.setX(center_x)
        result.snapped_x = True
        result.snap_target_x = "format_center"
        result.snap_line_x = center_x

    # Check center Y
    if abs(pos.y() - center_y) <= threshold:
        result.snapped_position.setY(center_y)
        result.snapped_y = True
        result.snap_target_y = "format_center"
        result.snap_line_y = center_y

    return result


def snap_to_bbox(
    pos: QPointF,
    bbox: tuple[float, float, float, float],  # x, y, width, height
    threshold: float = 5.0,
) -> SnapResult:
    """Snap position to a bbox (e.g., another node's output region).

    Bbox edges and center can be snapped to.

    Args:
        pos: Position to snap (in image pixel coordinates, top-left origin).
        bbox: Bounding box (x, y, width, height).
        threshold: Snapping threshold in pixels.

    Returns:
        SnapResult with snapped position and metadata.
    """
    x, y, w, h = bbox

    result = SnapResult(snapped_position=QPointF(pos.x(), pos.y()))

    # Check bbox edges
    # Left edge
    if abs(pos.x() - x) <= threshold:
        result.snapped_position.setX(x)
        result.snapped_x = True
        result.snap_target_x = "bbox_edge_left"
        result.snap_line_x = x
    # Right edge
    elif abs(pos.x() - (x + w)) <= threshold:
        result.snapped_position.setX(x + w)
        result.snapped_x = True
        result.snap_target_x = "bbox_edge_right"
        result.snap_line_x = x + w
    # Center X
    elif abs(pos.x() - (x + w / 2.0)) <= threshold:
        result.snapped_position.setX(x + w / 2.0)
        result.snapped_x = True
        result.snap_target_x = "bbox_center"
        result.snap_line_x = x + w / 2.0

    # Top edge
    if abs(pos.y() - y) <= threshold:
        result.snapped_position.setY(y)
        result.snapped_y = True
        result.snap_target_y = "bbox_edge_top"
        result.snap_line_y = y
    # Bottom edge
    elif abs(pos.y() - (y + h)) <= threshold:
        result.snapped_position.setY(y + h)
        result.snapped_y = True
        result.snap_target_y = "bbox_edge_bottom"
        result.snap_line_y = y + h
    # Center Y
    elif abs(pos.y() - (y + h / 2.0)) <= threshold:
        result.snapped_position.setY(y + h / 2.0)
        result.snapped_y = True
        result.snap_target_y = "bbox_center"
        result.snap_line_y = y + h / 2.0

    return result


def apply_snapping(
    pos: QPointF,
    config: SnapConfig,
    format_width: float,
    format_height: float,
    other_bboxes: Optional[list[tuple[float, float, float, float]]] = None,
) -> SnapResult:
    """Apply snapping based on configuration.

    Combines multiple snap sources and returns the best snap result.

    Args:
        pos: Position to snap (in image/format pixel coordinates).
        config: SnapConfig with snap settings.
        format_width: Format width in pixels.
        format_height: Format height in pixels.
        other_bboxes: Optional list of other bboxes to snap to.

    Returns:
        SnapResult with the best snapped position.
    """
    threshold = config.threshold_image

    # Start with no snap
    result = SnapResult(snapped_position=QPointF(pos.x(), pos.y()))

    # Apply format edge snapping
    if config.snap_to_format_edge:
        edge_result = snap_to_format_edge(pos, format_width, format_height, threshold)
        # Merge results - prefer format edge over no snap
        if edge_result.snapped_x:
            result.snapped_x = True
            result.snapped_position.setX(edge_result.snapped_position.x())
            result.snap_target_x = edge_result.snap_target_x
            result.snap_line_x = edge_result.snap_line_x
        if edge_result.snapped_y:
            result.snapped_y = True
            result.snapped_position.setY(edge_result.snapped_position.y())
            result.snap_target_y = edge_result.snap_target_y
            result.snap_line_y = edge_result.snap_line_y

    # Apply format center snapping (can override edge if closer)
    if config.snap_to_format_center:
        center_result = snap_to_format_center(pos, format_width, format_height, threshold)
        # Check if center is closer than edge (prefer closer snap)
        center_x = format_width / 2.0
        center_y = format_height / 2.0

        if center_result.snapped_x:
            # Compare distances
            if not result.snapped_x or abs(pos.x() - center_x) < abs(pos.x() - result.snapped_position.x()):
                result.snapped_x = True
                result.snapped_position.setX(center_result.snapped_position.x())
                result.snap_target_x = center_result.snap_target_x
                result.snap_line_x = center_result.snap_line_x

        if center_result.snapped_y:
            if not result.snapped_y or abs(pos.y() - center_y) < abs(pos.y() - result.snapped_position.y()):
                result.snapped_y = True
                result.snapped_position.setY(center_result.snapped_position.y())
                result.snap_target_y = center_result.snap_target_y
                result.snap_line_y = center_result.snap_line_y

    # Apply bbox snapping (optional)
    if config.snap_to_other_bbox and other_bboxes:
        for bbox in other_bboxes:
            bbox_result = snap_to_bbox(pos, bbox, threshold)
            # Merge - prefer bbox if closer
            if bbox_result.snapped_x:
                if not result.snapped_x or abs(pos.x() - bbox_result.snapped_position.x()) < abs(pos.x() - result.snapped_position.x()):
                    result.snapped_x = True
                    result.snapped_position.setX(bbox_result.snapped_position.x())
                    result.snap_target_x = bbox_result.snap_target_x
                    result.snap_line_x = bbox_result.snap_line_x

            if bbox_result.snapped_y:
                if not result.snapped_y or abs(pos.y() - bbox_result.snapped_position.y()) < abs(pos.y() - result.snapped_position.y()):
                    result.snapped_y = True
                    result.snapped_position.setY(bbox_result.snapped_position.y())
                    result.snap_target_y = bbox_result.snap_target_y
                    result.snap_line_y = bbox_result.snap_line_y

    return result


# ── Constraint System (S5-14) ────────────────────────────────────────────────

# Int bitmasks for Qt.KeyboardModifier members. PyQt6's strict enums reject
# mixed enum↔int bitwise arithmetic (TypeError), so modifier flags are
# normalized to plain ints (see _normalize_modifier_flags) and tested
# against these masks — safe for both int and Qt-enum inputs.
_SHIFT_MODIFIER_MASK = 0x2000000  # Qt.KeyboardModifier.ShiftModifier
_CONTROL_MODIFIER_MASK = 0x4000000  # Qt.KeyboardModifier.ControlModifier


def _normalize_modifier_flags(modifiers: Any) -> int:
    """Return an int modifier bitmask from an int OR a PyQt6 modifier enum."""
    if isinstance(modifiers, int):
        return modifiers
    return int(getattr(modifiers, "value", 0))


@dataclass
class ConstraintConfig:
    """Configuration for constraint behavior during drag.

    Constraints are applied via modifier keys:
    - Scale: aspect ratio locked BY DEFAULT; Shift frees the axes (independent x/y)
    - Ctrl: Lock to single axis (horizontal or vertical, depending on initial direction)
    - For rotate: Shift constrains to angle snaps (15°/45°/90°)

    Attributes:
        lock_aspect_ratio: Whether aspect ratio is locked (default for scale;
            cleared by holding Shift).
        lock_axis: Which axis is locked ('x', 'y', or None). 'x' means horizontal only, 'y' means vertical.
        angle_snap_degrees: List of angle snap values (default [15, 45, 90]).
        angle_snap_enabled: Whether angle snapping is enabled (Shift during rotate).
    """
    lock_aspect_ratio: bool = False
    lock_axis: Optional[str] = None  # 'x', 'y', or None
    angle_snap_degrees: list[float] = field(default_factory=lambda: [15.0, 45.0, 90.0])
    angle_snap_enabled: bool = False


def apply_aspect_ratio_constraint(
    new_size: tuple[float, float],  # width, height
    original_size: tuple[float, float],  # width, height
    aspect_ratio: Optional[float] = None,
) -> tuple[float, float]:
    """Apply aspect ratio constraint to size change.

    When aspect ratio is locked (the default for scale, cleared by Shift),
    maintains the original aspect ratio.

    Args:
        new_size: The unconstrained new size (width, height).
        original_size: The original size before drag (width, height).
        aspect_ratio: The aspect ratio to maintain (width / height). If None, computed from original.

    Returns:
        Constrained size (width, height) that maintains aspect ratio.
    """
    new_w, new_h = new_size
    orig_w, orig_h = original_size

    if aspect_ratio is None:
        if orig_h <= 0.0:
            return new_size
        aspect_ratio = orig_w / orig_h

    # Determine which dimension changed more significantly
    # and constrain the other to maintain aspect ratio
    if abs(new_w - orig_w) > abs(new_h - orig_h):
        # Width changed more - constrain height
        if new_w > 0.0:
            constrained_h = new_w / aspect_ratio
            return (new_w, constrained_h)
    else:
        # Height changed more - constrain width
        if new_h > 0.0:
            constrained_w = new_h * aspect_ratio
            return (constrained_w, new_h)

    return new_size


def apply_axis_constraint(
    delta: QPointF,
    locked_axis: Optional[str],
) -> QPointF:
    """Apply axis constraint to movement delta.

    When axis is locked (Ctrl key), movement is constrained to single axis.
    The locked axis is determined by the initial drag direction.

    Args:
        delta: The unconstrained movement delta.
        locked_axis: Which axis is locked ('x', 'y', or None).

    Returns:
        Constrained delta.
    """
    if locked_axis == 'x':
        # Horizontal only - zero out Y
        return QPointF(delta.x(), 0.0)
    elif locked_axis == 'y':
        # Vertical only - zero out X
        return QPointF(0.0, delta.y())
    else:
        return delta


def apply_angle_constraint(
    angle_degrees: float,
    snap_angles: list[float] = [15.0, 45.0, 90.0],
) -> float:
    """Apply angle constraint for rotation.

    When angle snapping is enabled (Shift during rotate), snaps to preset angles.

    Args:
        angle_degrees: The unconstrained angle in degrees.
        snap_angles: List of snap angles (default 15°, 45°, 90°).

    Returns:
        Snapped angle in degrees.
    """
    # Find the closest snap angle
    best_snap = None
    best_dist = float('inf')

    for snap in snap_angles:
        # Check both positive and negative
        for candidate in [snap, -snap]:
            dist = abs(angle_degrees - candidate)
            if dist < best_dist:
                best_dist = dist
                best_snap = candidate

    # Also check multiples of snap angles (e.g., 90, 180, 270, etc.)
    for snap in snap_angles:
        # Find nearest multiple
        if snap > 0.0:
            multiple = round(angle_degrees / snap) * snap
            dist = abs(angle_degrees - multiple)
            if dist < best_dist:
                best_dist = dist
                best_snap = multiple

    # Apply snap if close enough (threshold = half of smallest snap)
    if snap_angles:
        threshold = min(snap_angles) / 2.0
        if best_snap is not None and best_dist <= threshold:
            return best_snap

    return angle_degrees


def determine_locked_axis(
    delta: QPointF,
    threshold: float = 3.0,
) -> Optional[str]:
    """Determine which axis should be locked based on initial drag direction.

    Used when Ctrl key is pressed - locks to the axis with larger initial movement.

    Args:
        delta: Initial movement delta.
        threshold: Minimum delta to determine axis (avoid noise).

    Returns:
        'x', 'y', or None if both axes are within threshold.
    """
    dx = abs(delta.x())
    dy = abs(delta.y())

    if dx < threshold and dy < threshold:
        return None

    # Lock to the axis with larger movement
    if dx > dy:
        return 'x'
    else:
        return 'y'


def compute_constrained_delta(
    delta: QPointF,
    modifiers: int,
    original_size: Optional[tuple[float, float]] = None,
    is_scale: bool = False,
    is_rotate: bool = False,
    current_angle: float = 0.0,
    initial_delta: Optional[QPointF] = None,
) -> tuple[QPointF, ConstraintConfig, Optional[float]]:
    """Compute constrained delta based on modifier keys.

    This is a unified function that handles all constraint types.

    Args:
        delta: Current movement delta.
        modifiers: Qt.KeyboardModifier flags (Shift, Ctrl, etc).
        original_size: Original size for aspect ratio constraint (width, height).
        is_scale: Whether this is a scale operation (for aspect ratio).
        is_rotate: Whether this is a rotate operation (for angle constraint).
        current_angle: Current rotation angle (for angle constraint).
        initial_delta: Initial delta at drag start (for axis lock determination).

    Returns:
        Tuple of (constrained_delta, constraint_config, snapped_angle).
        snapped_angle is only set when is_rotate is True.
    """
    config = ConstraintConfig()
    constrained_delta = delta
    snapped_angle: Optional[float] = None

    # AUD-11401 (with the AUD-11306 enum defense): normalize Qt-enum
    # modifier inputs to an int bitmask (int & Qt enum raises TypeError in
    # PyQt6), then test against the int masks.
    modifiers = _normalize_modifier_flags(modifiers)
    shift_pressed = bool(modifiers & _SHIFT_MODIFIER_MASK)
    ctrl_pressed = bool(modifiers & _CONTROL_MODIFIER_MASK)

    if shift_pressed:
        if is_rotate:
            # Angle snapping
            config.angle_snap_enabled = True
            # Compute angle from delta (relative to center)
            # This is a simplified version - full implementation needs center position
            if delta.x() != 0.0 or delta.y() != 0.0:
                # Compute angle from delta direction
                import math
                angle_rad = math.atan2(delta.y(), delta.x())
                angle_deg = math.degrees(angle_rad)
                snapped_angle = apply_angle_constraint(angle_deg, config.angle_snap_degrees)
                # Convert back to delta (preserving magnitude)
                magnitude = math.sqrt(delta.x()**2 + delta.y()**2)
                snapped_rad = math.radians(snapped_angle)
                constrained_delta = QPointF(
                    magnitude * math.cos(snapped_rad),
                    magnitude * math.sin(snapped_rad)
                )
        # NOTE: Shift on a scale drag intentionally does NOT lock the aspect
        # ratio — see the elif below.
    elif is_scale and original_size:
        # AUD-11401: align with the CURRENT convention documented in
        # ConstraintConfig and implemented by get_constraint_config_from_modifiers
        # (the live pipeline used by composite_viewport): aspect ratio is
        # locked BY DEFAULT; Shift FREES the axes for independent x/y
        # scaling. The old branch here (Shift → lock) inverted that, so
        # wiring this "unified entry" would have flipped every scale drag.
        config.lock_aspect_ratio = True
        # Aspect ratio will be applied after computing new size

    # Ctrl key: axis lock
    if ctrl_pressed:
        if initial_delta is not None:
            # Determine locked axis from initial delta
            locked_axis = determine_locked_axis(initial_delta)
            config.lock_axis = locked_axis
            constrained_delta = apply_axis_constraint(constrained_delta, locked_axis)
        else:
            # Use current delta to determine
            locked_axis = determine_locked_axis(delta)
            config.lock_axis = locked_axis
            constrained_delta = apply_axis_constraint(constrained_delta, locked_axis)

    return (constrained_delta, config, snapped_angle)


# ── Modifier Key Detection (S5-14) ──────────────────────────────────────────

def get_constraint_config_from_modifiers(
    modifiers: int,
    is_scale: bool = False,
    is_rotate: bool = False,
) -> ConstraintConfig:
    """Create ConstraintConfig from Qt keyboard modifiers.

    Args:
        modifiers: Qt.KeyboardModifier flags.
        is_scale: Whether this is a scale operation.
        is_rotate: Whether this is a rotate operation.

    Returns:
        ConstraintConfig with appropriate settings.
    """
    config = ConstraintConfig()

    # AUD-11306 enum defense: accept int bitmasks and Qt modifier enums.
    modifiers = _normalize_modifier_flags(modifiers)
    shift_pressed = bool(modifiers & _SHIFT_MODIFIER_MASK)
    ctrl_pressed = bool(modifiers & _CONTROL_MODIFIER_MASK)

    if is_rotate and shift_pressed:
        # Rotate: Shift still enables angle snapping (unchanged).
        config.angle_snap_enabled = True
    elif is_scale and not shift_pressed:
        # Scale: aspect ratio is locked BY DEFAULT; Shift frees the axes for
        # independent x/y scaling (inverse of the old Shift-locks convention).
        config.lock_aspect_ratio = True

    # Ctrl: axis lock (determined later during drag)
    if ctrl_pressed:
        # Axis will be determined from initial delta
        pass

    return config


# ── Qt KeyboardModifier Import ──────────────────────────────────────────────
# Note: Qt.KeyboardModifier is imported from PyQt6.QtCore at the top of the file