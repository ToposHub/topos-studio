from types import SimpleNamespace

from src.features.composite.ui.composite_viewport import CompositeViewer


def _visible_bar(height: int):
    return SimpleNamespace(isVisible=lambda: True, height=lambda: height)


def test_composite_fit_reserves_toolbar_and_playback_controls() -> None:
    viewer = SimpleNamespace(
        width=lambda: 1000,
        height=lambda: 600,
        devicePixelRatio=lambda: 1.0,
        _toolbar=_visible_bar(28),
        video_control_bar=_visible_bar(74),
        _inspector_bar=SimpleNamespace(isVisible=lambda: False, height=lambda: 22),
        _zoom_level=1.0,
        _pan_x=0.0,
        _pan_y=0.0,
    )
    viewer._content_rect_pixels = lambda dpr: CompositeViewer._content_rect_pixels(viewer, dpr)

    x0, y0, x1, y1 = CompositeViewer._compute_vertices(viewer, 1000, 600, 1920, 1080)

    top_ndc = 1.0 - (28 / 600) * 2.0
    bottom_ndc = 1.0 - ((600 - 74) / 600) * 2.0
    assert y1 <= top_ndc + 1e-9
    assert y0 >= bottom_ndc - 1e-9
    assert x0 < x1
    assert y0 < y1


def test_pixel_rect_mapping_uses_same_content_rect_as_texture() -> None:
    viewer = SimpleNamespace(
        width=lambda: 1000,
        height=lambda: 600,
        devicePixelRatio=lambda: 1.0,
        _toolbar=_visible_bar(28),
        video_control_bar=_visible_bar(74),
        _inspector_bar=SimpleNamespace(isVisible=lambda: False, height=lambda: 22),
        _zoom_level=1.0,
        _pan_x=0.0,
        _pan_y=0.0,
    )
    viewer._content_rect_pixels = lambda dpr: CompositeViewer._content_rect_pixels(viewer, dpr)
    viewer._compute_vertices = lambda vp_w, vp_h, tex_w, tex_h: (
        CompositeViewer._compute_vertices(viewer, vp_w, vp_h, tex_w, tex_h))

    expected = CompositeViewer._compute_vertices(viewer, 1000, 600, 1920, 1080)
    mapped = CompositeViewer._ndc_from_pixel_rect(viewer, 0, 0, 1920, 1080, 1920, 1080)

    assert mapped == expected


def test_viewer3d_content_size_excludes_toolbar_and_playback_controls() -> None:
    viewer = SimpleNamespace(
        width=lambda: 1000,
        height=lambda: 600,
        devicePixelRatio=lambda: 1.0,
        _gl_initialized=True,
        _toolbar=_visible_bar(28),
        video_control_bar=_visible_bar(74),
        _inspector_bar=SimpleNamespace(isVisible=lambda: False, height=lambda: 22),
    )
    viewer._content_rect_pixels = lambda dpr: CompositeViewer._content_rect_pixels(viewer, dpr)

    assert CompositeViewer.viewer3d_content_size(viewer) == (1000, 498)


def test_viewer3d_render_region_uses_project_aspect_inside_content_rect() -> None:
    viewer = SimpleNamespace(
        width=lambda: 1000,
        height=lambda: 600,
        devicePixelRatio=lambda: 1.0,
        _gl_initialized=True,
        _toolbar=_visible_bar(28),
        video_control_bar=_visible_bar(74),
        _inspector_bar=SimpleNamespace(isVisible=lambda: False, height=lambda: 22),
        _composite_page=SimpleNamespace(_project_output_size=lambda: (1920, 1080)),
        _tex_width=1000,
        _tex_height=498,
    )
    viewer._content_rect_pixels = lambda dpr: CompositeViewer._content_rect_pixels(viewer, dpr)
    viewer._viewer3d_project_aspect = lambda: CompositeViewer._viewer3d_project_aspect(viewer)

    content = CompositeViewer._viewer3d_content_rect_widget(viewer)
    region = CompositeViewer._viewer3d_render_region_rect(viewer, content)

    assert region.width() == 885.3333333333333
    assert region.height() == 498
    assert region.top() == 28
