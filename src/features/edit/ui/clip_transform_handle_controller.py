"""Clip Transform on-canvas handle controller for the Edit page ProgramViewer.

Mirrors the composite page's Transform node handles (move / scale corners /
scale edges / rotate) and operates directly on the edit clip's **pixel-space**
``TransformParams`` model (translate_x/y, scale_x/y, rotation, pivot_mode,
pivot_x/y) — the exact same field model composite's
``compute_transform_handles`` / ``compute_transform_delta_params`` expect.

AUD-19730 (docstring accuracy): an earlier revision of this docstring claimed
the clip model was *normalized* (position_x/y, scale_x/y, anchor_x/y) and that
this controller bridged normalized ↔ pixel space (translate = position ×
canvas px, pivot = anchor × canvas px). The clip model has since been unified
with the composite Transform node's pixel-space fields, so **no bridging
happens here any more**: ``_params_dict`` is a direct field copy and
``_delta_to_transform`` applies pixel-space deltas straight onto the model.

This reuses ~all of composite's handle math without touching the clip data
model (which would ripple through serialization, animation, render plan, and
the identity-detection fast paths).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.features.edit.ui.program_viewer import ProgramViewer

import logging
from typing import Optional

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QPainter

from src.features.composite.ui.viewer_handles import (
    TransformHandlesData,
    compute_transform_handles,
    compute_transform_delta_params,
    get_transform_handle_definitions,
    find_nearest_handle,
    screen_pixels_to_ndc,
)
from src.features.edit.core.clip_params import TransformParams

logger = logging.getLogger(__name__)


def _point_in_polygon(x: float, y: float,
                      polygon: tuple) -> bool:
    """Ray-casting point-in-polygon test (pure Python, no numpy).

    ``polygon`` is a sequence of (x, y) vertex tuples. Robust to concave /
    self-intersecting frames produced by extreme rotations.
    """
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        # Check if the ray crosses edge (i, j).
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


class ClipTransformHandleController:
    """On-canvas Transform handle controller for a selected edit clip.

    Held by ``ProgramViewer``. Receives mouse events and draws handles.
    Reports param changes via callbacks (injected by the viewer) rather than
    mutating the clip model directly — the controller is model-agnostic.

    The clip is represented as ``(clip_id, TransformParams)`` plus the canvas
    (sequence resolution) size. Handles are drawn around the clip's
    transformed frame, exactly like composite's Transform node overlay.
    """

    def __init__(self, viewer: "ProgramViewer") -> None:
        self._viewer = viewer

        # Currently selected clip state.
        self._clip_id: Optional[str] = None
        self._transform: Optional[TransformParams] = None

        # Canvas (sequence resolution) — handle positions and the
        # NDC↔screen-pixel mapping both depend on this.
        self._canvas_width: int = 1920
        self._canvas_height: int = 1080

        # Drag state.
        self._active_handle: Optional[str] = None
        self._hover_handle: Optional[str] = None
        self._drag_start_screen: Optional[QPointF] = None
        self._drag_current_screen: Optional[QPointF] = None
        self._original_params: Optional[TransformParams] = None

        # Callbacks (injected by viewer; all optional).
        # on_drag_updated(clip_id, new_transform: TransformParams)
        # on_drag_ended(clip_id, final: TransformParams, original: TransformParams)
        self.on_drag_updated = None
        self.on_drag_ended = None

    # ── Public API ──

    def has_clip(self) -> bool:
        return self._clip_id is not None and self._transform is not None

    def set_clip(self, clip_id: Optional[str],
                 transform: Optional[TransformParams]) -> None:
        """Set the selected clip. ``None`` clears selection (hides handles)."""
        prev_id = self._clip_id
        self._clip_id = clip_id
        self._transform = transform
        if prev_id != clip_id:
            # Selection changed → reset any in-flight drag.
            self._reset_drag()
            self._hover_handle = None

    def set_transform(self, transform: Optional[TransformParams]) -> None:
        """Update the clip's transform (e.g. from properties panel) without
        changing the selection. Used to keep handle positions in sync when
        params change outside of a drag.
        """
        self._transform = transform

    def set_canvas_size(self, width: int, height: int) -> None:
        self._canvas_width = max(1, int(width))
        self._canvas_height = max(1, int(height))

    # ── Viewport geometry helpers (NDC ↔ widget/canvas pixels) ──

    def _get_image_ndc_rect(self) -> tuple[float, float, float, float]:
        """The video frame's rectangle in NDC, as displayed in the viewport."""
        try:
            x, y, w, h = self._viewer._calculate_display_rect()
            return (float(x), float(y), float(x + w), float(y + h))
        except Exception:
            return (-1.0, -1.0, 1.0, 1.0)

    def _get_device_pixel_ratio(self) -> float:
        try:
            screen = self._viewer.screen()
            if screen is not None:
                return float(screen.devicePixelRatio())
        except Exception:
            pass
        return 1.0

    def _params_dict(self, t: TransformParams) -> dict:
        """TransformParams → composite-viewer param dict (same model now).

        TransformParams is already pixel-space (translate_x/y, pivot_x/y in
        pixels, pivot_mode center/custom) — identical to what composite's
        ``compute_transform_handles`` / ``compute_transform_delta_params``
        expect, so this is a direct field copy with no conversion.
        """
        return {
            "translate_x": float(t.translate_x),
            "translate_y": float(t.translate_y),
            "scale_x": float(t.scale_x),
            "scale_y": float(t.scale_y),
            "rotation": float(t.rotation),
            "pivot_mode": t.pivot_mode,
            "pivot_x": float(t.pivot_x),
            "pivot_y": float(t.pivot_y),
        }

    def _delta_to_transform(
        self, delta: dict, base: TransformParams,
    ) -> TransformParams:
        """Apply a composite pixel-space delta dict onto ``base`` TransformParams.

        delta keys match TransformParams fields (translate_x/y, scale_x/y,
        rotation, pivot_mode, pivot_x/y). Missing keys keep ``base``'s value.
        """
        return TransformParams(
            translate_x=float(delta.get("translate_x", base.translate_x)),
            translate_y=float(delta.get("translate_y", base.translate_y)),
            scale_x=float(delta.get("scale_x", base.scale_x)),
            scale_y=float(delta.get("scale_y", base.scale_y)),
            rotation=float(delta.get("rotation", base.rotation)),
            pivot_mode=delta.get("pivot_mode", base.pivot_mode),
            pivot_x=float(delta.get("pivot_x", base.pivot_x)),
            pivot_y=float(delta.get("pivot_y", base.pivot_y)),
        )

    def _compute_handles(self) -> Optional[TransformHandlesData]:
        """Compute handle positions for the current clip's transform."""
        if self._transform is None:
            return None
        t = self._transform
        try:
            return compute_transform_handles(
                translate_x=float(t.translate_x),
                translate_y=float(t.translate_y),
                scale_x=float(t.scale_x),
                scale_y=float(t.scale_y),
                rotation=float(t.rotation),
                pivot_mode=t.pivot_mode,
                pivot_x=float(t.pivot_x),
                pivot_y=float(t.pivot_y),
                image_width=self._canvas_width,
                image_height=self._canvas_height,
                image_ndc_rect=self._get_image_ndc_rect(),
                canvas_width=self._canvas_width,
                canvas_height=self._canvas_height,
            )
        except Exception:
            logger.exception("Failed to compute clip transform handles")
            return None

    # ── Hit testing ──

    def hit_test(self, screen_pos: QPointF) -> Optional[str]:
        """Return the handle id under ``screen_pos``, or None."""
        if not self.has_clip():
            return None
        handles_data = self._compute_handles()
        if handles_data is None:
            return None
        try:
            defs = get_transform_handle_definitions(
                handles_data,
                hover_handle=self._hover_handle,
                active_handle=self._active_handle,
            )
            # The composite Transform node exposes its visible centre dot as
            # the *pivot* handle.  ``center`` is only a legacy/virtual move
            # handle there; translation starts by dragging the image body.
            # Both definitions occupy the same position in center-pivot mode,
            # so leaving ``center`` in this list makes it win the tie-breaker
            # in ``find_nearest_handle`` and incorrectly translates the clip
            # instead of moving the rotation/scale origin.
            defs = [definition for definition in defs
                    if definition.handle_type.value != "center"]
            dpr = self._get_device_pixel_ratio()
            return find_nearest_handle(
                screen_pos, defs,
                viewport_width=float(self._viewer.width()),
                viewport_height=float(self._viewer.height()),
                device_pixel_ratio=dpr,
                hit_radius=12.0,
            )
        except Exception:
            logger.exception("Clip handle hit_test failed")
            return None

    def hit_test_body(self, screen_pos: QPointF) -> bool:
        """True if ``screen_pos`` is inside the clip's transformed frame
        (for the move/translate gesture). Uses a pure-Python point-in-polygon
        ray-cast test (no numpy/matplotlib dependency)."""
        handles_data = self._compute_handles()
        if handles_data is None:
            return False
        try:
            corners = handles_data.frame_corners  # (TL, TR, BR, BL) in NDC
            dpr = self._get_device_pixel_ratio()
            ndc = screen_pixels_to_ndc(
                (screen_pos.x(), screen_pos.y()),
                float(self._viewer.width()), float(self._viewer.height()), dpr,
            )
            return _point_in_polygon(ndc[0], ndc[1], corners)
        except Exception:
            return False

    # ── Drag lifecycle ──

    def start_drag(self, handle_id: Optional[str], screen_pos: QPointF,
                   modifiers: int) -> bool:
        """Begin a drag. Returns True if a drag started (caller should
        accept the event and stop further processing)."""
        if not self.has_clip():
            return False
        # handle_id None → check body hit for a move gesture.
        if handle_id is None and not self.hit_test_body(screen_pos):
            return False
        self._active_handle = handle_id or "move"
        self._drag_start_screen = QPointF(screen_pos)
        self._drag_current_screen = QPointF(screen_pos)
        self._original_params = self._transform  # snapshot baseline
        return True

    def update_drag(self, screen_pos: QPointF, modifiers: int) -> None:
        """Update the drag (mouseMoveEvent). Computes the new transform and
        notifies the viewer via ``on_drag_updated``."""
        if not self.has_clip() or self._active_handle is None:
            return
        if self._drag_start_screen is None or self._original_params is None:
            return
        self._drag_current_screen = QPointF(screen_pos)

        start_img = self._screen_to_image_px(self._drag_start_screen)
        cur_img = self._screen_to_image_px(self._drag_current_screen)

        # composite delta math expects pixel-space original params — TransformParams
        # is already pixel-space now, so we pass its fields directly.
        params_for_calc = self._params_dict(self._original_params)
        try:
            delta = compute_transform_delta_params(
                handle_id=self._active_handle,
                drag_start_image_pos=start_img,
                drag_current_image_pos=cur_img,
                original_params=params_for_calc,
                image_width=self._canvas_width,
                image_height=self._canvas_height,
                modifiers=modifiers,
                max_scale=5.0,  # TransformParams caps scale at 5.0 (matches TransformNode)
            )
        except Exception:
            logger.exception("compute_transform_delta_params failed for clip")
            return
        if not delta:
            return

        new_transform = self._delta_to_transform(delta, self._original_params)
        # Keep the live transform in sync so the next frame's handles track.
        self._transform = new_transform
        # Repaint the lightweight overlay before the host applies the
        # immutable timeline update.  The host callback also requests the
        # rendered frame, but that path is asynchronous and can otherwise
        # make the handles feel stuck until the next playback repaint.
        try:
            self._viewer.update()
        except (AttributeError, RuntimeError):
            pass
        if self.on_drag_updated is not None:
            try:
                self.on_drag_updated(self._clip_id, new_transform)
            except Exception:
                logger.exception("on_drag_updated callback failed")

    def end_drag(self) -> None:
        """End the drag. Notifies the viewer with the final transform for the
        undo command."""
        if not self.has_clip() or self._active_handle is None:
            self._reset_drag()
            return
        if self._original_params is not None and self._transform is not None \
                and self._transform != self._original_params:
            if self.on_drag_ended is not None:
                try:
                    self.on_drag_ended(
                        self._clip_id, self._transform, self._original_params,
                    )
                except Exception:
                    logger.exception("on_drag_ended callback failed")
        self._reset_drag()

    def update_hover(self, screen_pos: QPointF) -> None:
        if not self.has_clip() or self._active_handle is not None:
            return
        new_hover = self.hit_test(screen_pos)
        if new_hover != self._hover_handle:
            self._hover_handle = new_hover
            self._viewer.update()

    def _reset_drag(self) -> None:
        self._active_handle = None
        self._drag_start_screen = None
        self._drag_current_screen = None
        self._original_params = None

    # ── Coordinate conversion ──

    def _screen_to_image_px(self, screen_pos: QPointF) -> tuple[float, float]:
        """Widget pixels → canvas pixel space (top-left origin, Y-down).

        This MUST match the coordinate space that composite's
        ``compute_transform_delta_params`` and ``_handle_local_anchor`` expect:
        origin at the image's TOP-LEFT corner, +X right, +Y down, units = image
        (canvas) pixels, range [0, w] × [0, h]. Using a center-origin here
        breaks scale/rotate handles because the anchor math in
        ``_handle_local_anchor`` (TL=(0,0), BR=(w,h), center=(w/2,h/2)) is
        absolute, not relative to center.
        """
        ndc_rect = self._get_image_ndc_rect()
        dpr = self._get_device_pixel_ratio()
        ndc = screen_pixels_to_ndc(
            (screen_pos.x(), screen_pos.y()),
            float(self._viewer.width()), float(self._viewer.height()), dpr,
        )
        rx0, ry0, rx1, ry1 = ndc_rect
        rect_w = rx1 - rx0
        rect_h = ry1 - ry0
        if abs(rect_w) < 1e-6 or abs(rect_h) < 1e-6:
            return (0.0, 0.0)
        # NDC (Y-up) → normalized [0,1] within the displayed canvas rect.
        norm_x = (ndc[0] - rx0) / rect_w
        norm_y_up = (ndc[1] - ry0) / rect_h
        # Flip Y to image space (Y-down): top = 0, bottom = h.
        norm_y_down = 1.0 - norm_y_up
        # Map [0,1] → [0, w/h] in canvas pixels (top-left origin).
        px = norm_x * self._canvas_width
        py = norm_y_down * self._canvas_height
        return (px, py)

    # ── Drawing ──

    def draw_handles(self, painter: QPainter) -> None:
        """Draw the transform overlay: frame outline + handle glow dots.

        Called from ProgramViewer.paintGL inside a QPainter block. Uses
        QPainter (not raw GL) to match the subtitle handle drawing style.
        """
        if not self.has_clip():
            return
        handles_data = self._compute_handles()
        if handles_data is None:
            return
        # Rotated outlines and circular hit markers are sub-pixel geometry.
        # Explicitly enable QPainter AA here rather than relying on the widget
        # default, which is disabled after a native-GL painting section on
        # several macOS drivers.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._draw_frame_outline(painter, handles_data)
        self._draw_handle_points(painter, handles_data)

    def _ndc_to_view_px(self, ndc_x: float, ndc_y: float) -> tuple[float, float]:
        """NDC → widget pixel coordinates for QPainter drawing."""
        dpr = self._get_device_pixel_ratio()
        vw = float(self._viewer.width())
        vh = float(self._viewer.height())
        # screen_pixels_to_ndc maps widget px → NDC; invert it.
        # NDC x = (2*px/dpr)/vw - 1  →  px = (ndc_x+1)*vw*dpr/2
        px = (ndc_x + 1.0) * vw * dpr / 2.0
        # NDC y is up, widget y is down.
        py = (1.0 - ndc_y) * vh * dpr / 2.0
        return (px / dpr, py / dpr)

    def _draw_frame_outline(self, painter: QPainter,
                            handles_data: TransformHandlesData) -> None:
        """Draw the transformed frame outline + rotate guide line.

        Mirrors composite ``_draw_transform_auxiliary``: the rotated rectangle
        through the 4 transformed corners, plus a thin line from the pivot to
        the rotate handle so the rotation axis is visually clear.
        """
        from PyQt6.QtGui import QColor, QPen, QPainterPath
        corners = handles_data.frame_corners  # (TL, TR, BR, BL) NDC
        # Rotated frame outline — thin, semi-transparent white (matches
        # composite's transform frame, not the bright bbox).
        path = QPainterPath()
        for i, (nx, ny) in enumerate(corners):
            px, py = self._ndc_to_view_px(nx, ny)
            if i == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        path.closeSubpath()
        pen = QPen(QColor(255, 255, 255, 140), 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # Rotate guide: pivot → rotate handle.  This is intentionally the
        # same anchor used by the composite Transform node; when a custom
        # pivot is moved off the frame centre the guide must follow it.
        cx, cy = self._ndc_to_view_px(*handles_data.pivot)
        rx, ry = self._ndc_to_view_px(*handles_data.rotate)
        guide_pen = QPen(QColor(0, 255, 255, 160), 1.0)
        guide_pen.setCosmetic(True)
        guide_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(guide_pen)
        painter.drawLine(QPointF(cx, cy), QPointF(rx, ry))

    def _draw_handle_points(self, painter: QPainter,
                            handles_data: TransformHandlesData) -> None:
        """Draw all handles as glow dots — composite TransformNode style.

        Replicates ``CompositeViewer._draw_handles_from_dict`` for transform
        handles: every handle is a glow dot (bright white core + soft blue
        halo). Active (dragging) → orange core/glow; hover → warm yellow.
        Center/pivot/rotate use a slightly larger radius (4.7) than edge/
        corner handles (3.3), matching composite's sizing.
        """
        from PyQt6.QtGui import QColor, QBrush, QRadialGradient

        # handle_id → (ndc_pos, is_large_radius).  The composite Transform
        # node displays one centre dot: the pivot / rotation-and-scale anchor.
        # It deliberately does not draw the legacy virtual ``center`` move
        # handle, because translation is performed by dragging the frame body.
        handle_defs = [
            ("pivot",             handles_data.pivot,             True),
            ("rotate",            handles_data.rotate,            True),
            ("scale_corner_tl",   handles_data.scale_corner_tl,   False),
            ("scale_corner_tr",   handles_data.scale_corner_tr,   False),
            ("scale_corner_bl",   handles_data.scale_corner_bl,   False),
            ("scale_corner_br",   handles_data.scale_corner_br,   False),
            ("scale_x_left",      handles_data.scale_x_left,      False),
            ("scale_x_right",     handles_data.scale_x_right,     False),
            ("scale_y_top",       handles_data.scale_y_top,       False),
            ("scale_y_bottom",    handles_data.scale_y_bottom,    False),
        ]
        active = self._active_handle
        hover = self._hover_handle

        for handle_id, (nx, ny), is_large in handle_defs:
            px, py = self._ndc_to_view_px(nx, ny)
            is_active = active == handle_id
            is_hovered = hover == handle_id

            # Glow colors: default blue halo + white core; active=orange;
            # hover=warm yellow (verbatim from composite _draw_handles_from_dict).
            if is_active:
                core_color = QColor(255, 191, 77, 255)     # (1.0, 0.75, 0.3, 1.0)
                glow_color = QColor(255, 128, 0, 140)      # (1.0, 0.5, 0.0, 0.55)
            elif is_hovered:
                core_color = QColor(255, 242, 153, 255)    # (1.0, 0.95, 0.6, 1.0)
                glow_color = QColor(255, 217, 51, 153)     # (1.0, 0.85, 0.2, 0.6)
            else:
                core_color = QColor(255, 255, 255, 242)    # (1.0, 1.0, 1.0, 0.95)
                glow_color = QColor(102, 191, 255, 140)    # (0.4, 0.75, 1.0, 0.55)

            # Radii match composite: 4.7 for center/rotate, 3.3 for the rest.
            core_r = 4.7 if is_large else 3.3
            glow_r = core_r * 2.0

            # Halo: radial gradient fading to transparent at the edge.
            gradient = QRadialGradient(px, py, glow_r)
            gradient.setColorAt(0.0, glow_color)
            end_color = QColor(glow_color)
            end_color.setAlpha(0)
            gradient.setColorAt(1.0, end_color)
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(px, py), glow_r, glow_r)

            # Core: solid dot.
            painter.setBrush(QBrush(core_color))
            painter.drawEllipse(QPointF(px, py), core_r, core_r)
