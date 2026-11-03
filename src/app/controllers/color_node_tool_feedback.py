"""Read-only live tool markers for existing per-layer parameter editors."""
from __future__ import annotations

from typing import Any
from PyQt6.QtCore import QObject
from src.features.color.ui.node_tool_indicators import detect_tool_indicators


class ColorNodeToolFeedback(QObject):
    """Refresh the selected card after an existing editor has changed its layer.

    This never submits a project command or decodes/renders pixels. Authoritative
    projection remains responsible for saved, inactive and shared node markers.
    """

    def __init__(self, host: Any) -> None:
        super().__init__(host)
        self._host = host
        watchers = (
            ("grading_panel", "grading_changed"), ("grading_panel", "tonal_saturation_changed"),
            ("curves_widget", "changed"), ("mixer_panel", "mixer_changed"),
            ("warper_panel", "warper_changed"), ("effects_panel", "effects_changed"),
            ("qualifier_panel", "params_changed"), ("lut_panel", "lut_selected"),
            ("lut_panel", "lut_removed"), ("lut_panel", "lut_reset"),
            ("lut_panel", "intensity_changed"), ("mask_panel", "shape_added"),
            ("mask_panel", "shape_removed"), ("mask_panel", "shape_updated"),
            ("mask_panel", "all_masks_cleared"), ("engine", "mask_shapes_changed"),
        )
        for name, signal_name in watchers:
            signal = getattr(getattr(host, name, None), signal_name, None)
            if signal is not None:
                signal.connect(self.refresh)
        hsl = getattr(getattr(host, "hsl_curves_panel", None), "hsl_curves_widget", None)
        if hsl is not None:
            hsl.changed.connect(self.refresh)

    def refresh(self, *_args: Any) -> None:
        host = self._host
        if getattr(host, "_color_stack_hydration_in_progress", False):
            return
        graph_host = getattr(host, "color_graph_host", None)
        handle = graph_host.panel_host.active_handle if graph_host else None
        if handle is None or not handle.is_corrector:
            return
        layer = host.engine.get_active_layer()
        payload = graph_host._node_payload(handle.node_id) or {}
        if layer is None or layer.id != payload.get("id"):
            return  # Never attribute a root layer's tools to a nested node.
        card = host.color_node_panel.view.cards.get(handle.node_id)
        if card is None:
            return
        indicators = detect_tool_indicators(layer.to_dict(None))
        if card.node.tool_indicators != indicators:
            card.node.tool_indicators = indicators
            card.setToolTip(card.toolTip())
            card.update()
