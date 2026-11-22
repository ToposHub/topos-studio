"""B10: channel selection and data-window propagation.

Premultiplied channel restore (alpha=0, negative and HDR values), half-
open windows with empty/negative support, per-node propagation (blur
halo, crop intersection, transform corners + sampling radius, merge
union), and window-aware cache keys separating ROI from full frame.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest
from tests.composite.standalone_gpu import run_standalone_gpu

from src.features.composite.core.channel_windows import (
    EMPTY_WINDOW, ChannelSelection, DataWindow, apply_channel_selection,
    merge_window, transform_window, window_cache_key,
)

pytestmark = pytest.mark.composite_core


def _rgba(*pixels):
    return np.array(pixels, dtype=np.float32).reshape(-1, 1, 4)


# ── Channel selection ───────────────────────────────────────────────────────


def test_alpha_only_selection_restores_colors_from_input():
    source = _rgba((0.2, 0.4, 0.6, 0.5))
    effect = _rgba((9.0, 9.0, 9.0, 0.8))
    result = apply_channel_selection(
        effect, source, ChannelSelection(red=False, green=False, blue=False))
    # Preserve input straight color (0.4, 0.8, 1.2) at the updated alpha.
    assert result[0, 0].tolist() == pytest.approx([0.32, 0.64, 0.96, 0.8])


def test_color_only_selection_restores_alpha_from_input():
    source = _rgba((0.2, 0.4, 0.6, 0.5))
    effect = _rgba((1.0, 0.0, 0.0, 0.9))
    result = apply_channel_selection(
        effect, source,
        ChannelSelection(red=True, green=True, blue=True, alpha=False))
    assert result[0, 0].tolist() == pytest.approx([0.5 / 0.9, 0.0, 0.0, 0.5])


def test_mixed_channels_use_their_own_alpha_and_preserve_hdr():
    source = _rgba((0.4, -1.0, 2.0, 0.25), (0.0, 0.0, 0.0, 0.0))
    effect = _rgba((1.0, 2.0, 3.0, 0.50), (4.0, 3.0, 2.0, 0.50))
    result = apply_channel_selection(effect, source, ChannelSelection(True, False, False, True))
    np.testing.assert_allclose(result[:, 0], [[1, -2, 4, 0.5], [4, 0, 0, 0.5]])
    result = apply_channel_selection(effect, source, ChannelSelection(True, False, False, False))
    np.testing.assert_allclose(result[:, 0], [[0.5, -1, 2, 0.25], [0, 0, 0, 0]])


def test_partial_color_selection_restores_only_unselected():
    source = _rgba((0.1, 0.2, 0.3, 1.0))
    effect = _rgba((1.0, 1.0, 1.0, 1.0))
    result = apply_channel_selection(
        effect, source, ChannelSelection(red=True, green=False, blue=False))
    assert result[0, 0].tolist() == pytest.approx([1.0, 0.2, 0.3, 1.0])


def test_selection_preserves_alpha_zero_and_negative_and_hdr():
    source = _rgba((0.0, 0.0, 0.0, 0.0),
                   (-2.0, -4.0, 8.0, 0.5),
                   (16.0, 0.0, 0.0, 1.0))
    effect = _rgba((1.0, 1.0, 1.0, 1.0),
                   (1.0, 1.0, 1.0, 1.0),
                   (1.0, 1.0, 1.0, 1.0))
    result = apply_channel_selection(
        effect, source,
        ChannelSelection(red=False, green=False, blue=False, alpha=False))
    np.testing.assert_allclose(result.reshape(3, 4), source.reshape(3, 4))


def test_select_all_is_passthrough():
    source = _rgba((0.3, 0.3, 0.3, 1.0))
    out = apply_channel_selection(source * 2, source, ChannelSelection())
    assert out[0, 0, 0] == pytest.approx(0.6)  # untouched effect output


def test_shape_mismatch_raises():
    with pytest.raises(ValueError):
        apply_channel_selection(np.zeros((2, 1, 4), np.float32),
                                np.zeros((1, 1, 4), np.float32),
                                ChannelSelection())
    with pytest.raises(ValueError):
        apply_channel_selection(np.zeros((2, 1, 3), np.float32),
                                np.zeros((2, 1, 3), np.float32),
                                ChannelSelection())


# ── Data windows ────────────────────────────────────────────────────────────


def test_windows_support_empty_and_negative_coordinates():
    empty = DataWindow(5, 5, 5, 9)  # zero width though non-zero height
    assert empty.is_empty and empty.size[0] == 0
    negative = DataWindow(-100, -50, -40, -10)
    assert not negative.is_empty
    assert negative.size == (60, 40)
    assert DataWindow(0, 0, 0, 0).is_empty


def test_window_union_and_intersection():
    a = DataWindow(0, 0, 10, 10)
    b = DataWindow(5, 5, 15, 15)
    assert a.union(b) == DataWindow(0, 0, 15, 15)
    assert a.intersect(b) == DataWindow(5, 5, 10, 10)
    disjoint = a.intersect(DataWindow(20, 20, 30, 30))
    assert disjoint.is_empty
    # Union with empty keeps the other operand.
    assert EMPTY_WINDOW.union(a) == a
    assert a.union(EMPTY_WINDOW) == a


def test_blur_window_expands_by_halo():
    window = DataWindow(10, 10, 50, 40)
    assert window.expanded(0) == window
    assert window.expanded(2.5) == DataWindow(7, 7, 53, 43)  # ceil(2.5)=3
    assert EMPTY_WINDOW.expanded(3) == EMPTY_WINDOW
    assert DataWindow(-5, 2, -5, 10).expanded(3).is_empty


def test_crop_window_intersects():
    canvas = DataWindow(0, 0, 1920, 1080)
    layer = DataWindow(-64, -32, 512, 512)
    assert canvas.intersect(layer) == DataWindow(0, 0, 512, 512)


def test_transform_window_bounding_box_with_sampling_radius():
    window = DataWindow(0, 0, 100, 100)
    # 90° rotation of the four corners.
    corners = ((0, 100), (100, 100), (100, 0), (0, 0))
    rotated = transform_window(window, corners, sampling_radius=1.5)
    assert rotated == DataWindow(-2, -2, 102, 102)  # pad = ceil(1.5) = 2
    # Translation only.
    moved = transform_window(window, ((110, 210), (210, 210),
                                      (210, 110), (110, 110)))
    assert moved == DataWindow(110, 110, 210, 210)
    # Empty input stays empty.
    assert transform_window(EMPTY_WINDOW, ()) == EMPTY_WINDOW


def test_merge_window_unions_by_mode():
    fg = DataWindow(10, 10, 40, 40)
    bg = DataWindow(0, 0, 30, 60)
    for mode in ("over", "add", "multiply", "screen"):
        assert merge_window(fg, bg, mode) == DataWindow(0, 0, 40, 60)


def test_window_cache_key_separates_roi_from_full_frame():
    full = DataWindow(0, 0, 3840, 2160)
    roi = DataWindow(512, 512, 1024, 1024)
    key_full = window_cache_key(full, 7, "preview", "rev1")
    key_roi = window_cache_key(roi, 7, "preview", "rev1")
    assert key_full != key_roi
    assert key_full != window_cache_key(full, 8, "preview", "rev1")
    assert key_full != window_cache_key(full, 7, "export", "rev1")
    assert key_full != window_cache_key(full, 7, "preview", "rev2")
    assert key_full == window_cache_key(full, 7, "preview", "rev1")
    assert {key_full: "full", key_roi: "tile"}[key_roi] == "tile"
    assert window_cache_key(full, 7, None, 1) != window_cache_key(full, 7, "None", "1")


def test_window_round_trips_through_dict():
    window = DataWindow(-10, 4, 128, 256)
    assert DataWindow.from_dict(window.to_dict()) == window
    assert DataWindow.from_dict("junk") == EMPTY_WINDOW
    assert DataWindow.from_dict({"x1": float("inf")}) == EMPTY_WINDOW


def test_gpu_blur_channel_restore_matches_the_premultiplied_reference() -> None:
    run_standalone_gpu(_check_gpu_blur_channel_restore_matches_the_premultiplied_reference)


def _check_gpu_blur_channel_restore_matches_the_premultiplied_reference() -> None:
    from src.features.composite.core.node_capabilities import get_node_capability
    from src.features.composite.nodes.base_node import TextureResult
    from src.features.composite.render.composite_export_renderer import CompositeExportRenderer

    moderngl = pytest.importorskip("moderngl")
    try:
        probe = moderngl.create_standalone_context()
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"No standalone GL context: {exc}")
    probe.release()
    renderer = CompositeExportRenderer()
    assert get_node_capability("blur").supports_channel_selection
    try:
        renderer._ensure_gpu_resources()
        source = _rgba((0, 0, 0, 0), (0.5, -1, 2, 0.5), (1, 0, 3, 1)).reshape(1, 3, 4)
        texture = renderer._resource_pool.acquire_texture(3, 1, dtype="f4")
        texture.write(source.tobytes())
        inp = TextureResult(
            texture_id=texture.glo, width=3, height=1,
            alpha_premultiplied=True, color_space="linear",
        )
        params = {"radius_x": 1.0, "radius_y": 0.0, "channel_a": True, "mix": 0.5}

        def render(values):
            output = renderer._apply_blur(inp, values, 3, 1)["result"]
            out_tex = renderer._get_texture_by_id(output.texture_id)
            framebuffer = renderer.fb_manager.get_fbo(out_tex)
            return np.frombuffer(framebuffer.read(components=4, dtype="f4"), np.float32).reshape(1, 3, 4)

        full_effect = render(params)
        selected = render({**params, "channel_r": False, "channel_g": False, "channel_b": False})
        expected = apply_channel_selection(full_effect, source, ChannelSelection(False, False, False, True))
        np.testing.assert_allclose(selected, expected, atol=0.002)
        assert selected[0, 1, 1] < 0 and selected[0, 1, 2] > 1
    finally:
        renderer.cleanup()


def test_preview_roi_requires_full_frame_for_nonlocal_blur():
    from src.features.composite.core.format import BBox
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.render.render_scheduler import RenderScheduler

    graph = NodeGraph()
    blur = BlurNode("blur")
    graph.add_node(blur)
    scheduler = RenderScheduler()
    roi = BBox(-8, 4, 16, 32)
    assert scheduler.plan(graph.snapshot(), "preview", viewer_roi=roi).roi_enabled
    blur.set_parameter("edge_policy", "wrap")
    assert not scheduler.plan(graph.snapshot(), "preview", viewer_roi=roi).roi_enabled


def test_gpu_tiled_blur_matches_full_frame_across_padding() -> None:
    run_standalone_gpu(_check_gpu_tiled_blur_matches_full_frame_across_padding)


def _check_gpu_tiled_blur_matches_full_frame_across_padding() -> None:
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.render.composite_export_renderer import CompositeExportRenderer
    from src.features.composite.render.render_scheduler import RenderScheduler

    moderngl = pytest.importorskip("moderngl")
    try:
        probe = moderngl.create_standalone_context()
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"No standalone GL context: {exc}")
    probe.release()
    graph = NodeGraph()
    source = ConstantNode("source")
    blur = BlurNode("blur")
    output = OutputNode("output")
    for key, value in {
        "width": 80, "height": 48, "mode": "checker", "checker_size": 11,
        "color_r": 3.0, "color_a": 0.4, "color2_a": 0.75,
    }.items():
        source.set_parameter(key, value)
    blur.set_parameter("radius_x", 3.5)
    blur.set_parameter("radius_y", 3.5)
    for node in (source, blur, output):
        graph.add_node(node)
    assert graph.connect(source.id, "result", blur.id, "image")
    assert graph.connect(blur.id, "result", output.id, "image")
    snapshot = graph.snapshot(color_width=80, color_height=48)
    plan = RenderScheduler().plan(snapshot, "export")
    plan.tile_size = 24
    assert plan.estimated_padding >= 4
    renderer = CompositeExportRenderer()
    try:
        renderer.set_persistent_mode(True)
        full = renderer.render_and_readback(snapshot)
        tiled = renderer._render_tiled_with_plan(snapshot, 80, 48, plan)
        assert full.error is None, full.error
        assert tiled.error is None, tiled.error
        assert tiled.tile_count == 8
        np.testing.assert_allclose(tiled.image, full.image, atol=0.002)
    finally:
        renderer.cleanup()
