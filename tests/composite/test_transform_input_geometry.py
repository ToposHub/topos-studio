"""Regression coverage for Transform input-vs-canvas geometry."""

from __future__ import annotations

from types import SimpleNamespace

from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.composite_viewport import CompositeViewer
from src.features.composite.ui.viewer_handles import compute_transform_handles


class _Graph:
    """Small graph double used without constructing a QWidget."""

    def __init__(self, source: object) -> None:
        self._source = source

    def get_input_connections(self, node_id: str) -> list[SimpleNamespace]:
        assert node_id == "transform"
        return [
            SimpleNamespace(
                dst_port_name="image",
                src_node_id="color",
            )
        ]

    def get_node(self, node_id: str) -> object:
        assert node_id == "color"
        return self._source


class _PageProbe:
    """Bind the two page helpers without initializing the QWidget tree."""

    _resolve_node_input_size = CompositePage._resolve_node_input_size
    _resolve_transform_interaction_size = CompositePage._resolve_transform_interaction_size


def test_color_input_geometry_uses_live_snapshot_dimensions() -> None:
    """UI graph COLOR nodes do not own the renderer's cached texture size."""
    source = SimpleNamespace(
        node_type="color_input",
        _cached_width=0,
        _cached_height=0,
        parameters={},
    )
    page = _PageProbe()
    page._node_graph = _Graph(source)
    page._last_color_width = 1500
    page._last_color_height = 800
    page._last_texture_width = 1920
    page._last_texture_height = 1080
    transform = SimpleNamespace(id="transform")

    assert CompositePage._resolve_node_input_size(page, transform) == (1500, 800)
    assert CompositePage._resolve_transform_interaction_size(page, transform) == (1500.0, 800.0)


def test_viewer_transform_size_prefers_input_over_composite_canvas() -> None:
    """All handle paths share the Transform input dimensions."""
    viewer = SimpleNamespace(
        _selected_node_input_size=(1500, 800),
        _tex_width=1920,
        _tex_height=1080,
    )

    assert CompositeViewer._selected_transform_input_size(viewer) == (1500.0, 800.0)


def test_4k_fit_transform_handles_cover_2k_composition() -> None:
    """Native 4K geometry plus fit params maps exactly to the 2K canvas."""
    handles = compute_transform_handles(
        translate_x=-960.0,
        translate_y=540.0,
        scale_x=0.5,
        scale_y=0.5,
        rotation=0.0,
        pivot_mode="center",
        pivot_x=1920.0,
        pivot_y=1080.0,
        image_width=3840,
        image_height=2160,
        image_ndc_rect=(-1.0, -1.0, 1.0, 1.0),
        canvas_width=1920,
        canvas_height=1080,
    )

    assert handles.frame_corners == (
        (-1.0, 1.0), (1.0, 1.0), (1.0, -1.0), (-1.0, -1.0)
    )
