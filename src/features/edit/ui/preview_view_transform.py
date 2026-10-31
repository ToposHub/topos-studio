from dataclasses import dataclass


@dataclass
class PreviewViewTransform:
    """Shared zoom/pan state for ProgramViewer and SourceViewer.

    Modes:
    - "fit": Auto-fit to viewport. Resize recalculates.
    - "manual": User zoom/pan. Resize preserves.
    - "actual": 1:1 pixel size.
    """
    zoom: float = 1.0
    pan_x_px: float = 0.0
    pan_y_px: float = 0.0
    mode: str = "fit"
    min_zoom: float = 0.05
    max_zoom: float = 16.0

    def reset_fit(self) -> None:
        self.zoom = 1.0
        self.pan_x_px = 0.0
        self.pan_y_px = 0.0
        self.mode = "fit"

    def set_actual_size(self, texture_w: int, texture_h: int, viewport_w: int, viewport_h: int) -> None:
        if texture_w <= 0 or texture_h <= 0 or viewport_w <= 0 or viewport_h <= 0:
            return
        fit_scale = min(viewport_w / texture_w, viewport_h / texture_h)
        self.zoom = 1.0 / fit_scale if fit_scale > 0 else 1.0
        self.pan_x_px = 0.0
        self.pan_y_px = 0.0
        self.mode = "actual"

    def zoom_at(self, factor: float, anchor_x: float, anchor_y: float,
                viewport_w: int, viewport_h: int, texture_w: int, texture_h: int) -> None:
        if texture_w <= 0 or texture_h <= 0 or viewport_w <= 0 or viewport_h <= 0:
            return

        fit_scale = min(viewport_w / texture_w, viewport_h / texture_h)
        fit_w = texture_w * fit_scale
        fit_h = texture_h * fit_scale

        old_w = fit_w * self.zoom
        old_h = fit_h * self.zoom
        old_x = (viewport_w - old_w) / 2 + self.pan_x_px
        old_y = (viewport_h - old_h) / 2 + self.pan_y_px

        if old_w <= 0 or old_h <= 0:
            return

        rel_x = (anchor_x - old_x) / old_w
        rel_y = (anchor_y - old_y) / old_h

        new_zoom = max(self.min_zoom, min(self.max_zoom, self.zoom * factor))
        new_w = fit_w * new_zoom
        new_h = fit_h * new_zoom

        self.pan_x_px = anchor_x - viewport_w / 2 - (rel_x - 0.5) * new_w
        self.pan_y_px = anchor_y - viewport_h / 2 - (rel_y - 0.5) * new_h
        self.zoom = new_zoom
        self.mode = "manual"

    def pan_by(self, dx: float, dy: float) -> None:
        self.pan_x_px += dx
        self.pan_y_px += dy
        self.mode = "manual"

    def display_rect_px(self, texture_w: int, texture_h: int,
                        viewport_w: int, viewport_h: int) -> tuple[float, float, float, float]:
        if texture_w <= 0 or texture_h <= 0 or viewport_w <= 0 or viewport_h <= 0:
            return 0, 0, viewport_w, viewport_h
        fit_scale = min(viewport_w / texture_w, viewport_h / texture_h)
        display_w = texture_w * fit_scale * self.zoom
        display_h = texture_h * fit_scale * self.zoom
        x = (viewport_w - display_w) / 2 + self.pan_x_px
        y = (viewport_h - display_h) / 2 + self.pan_y_px
        return x, y, display_w, display_h

    def display_rect_ndc(self, texture_w: int, texture_h: int,
                         viewport_w: int, viewport_h: int) -> tuple[float, float, float, float]:
        x, y, w, h = self.display_rect_px(texture_w, texture_h, viewport_w, viewport_h)
        if viewport_w <= 0 or viewport_h <= 0:
            return -1, -1, 2, 2
        ndc_x = x / viewport_w * 2.0 - 1.0
        ndc_y = 1.0 - (y + h) / viewport_h * 2.0
        ndc_w = w / viewport_w * 2.0
        ndc_h = h / viewport_h * 2.0
        return ndc_x, ndc_y, ndc_w, ndc_h
