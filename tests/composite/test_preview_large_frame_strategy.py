"""P0-1: oversized preview frames must not take the per-tile linear f32
readback path (4 x 2048² sync readbacks + numpy stitch ≈ 500ms/frame at
4K). Preview (> TILE_THRESHOLD) picks full-frame or GPU-stitched tile via
the pool budget; export keeps the legacy linear path.
"""

import numpy as np
import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.render.composite_export_renderer import (
    CompositeExportRenderer,
)

pytestmark = pytest.mark.composite_core

# max dim > TILE_THRESHOLD (2048) → plan.needs_tiling is true
W, H = 2560, 1440
SOLID_COLOR = (0.5, 0.25, 0.125, 1.0)


def _solid_graph() -> NodeGraph:
    data = {
        "nodes": [
            {"id": "bg", "type": "constant", "parameters": {
                "width": W, "height": H, "mode": "solid",
                "color": list(SOLID_COLOR)}},
            {"id": "out", "type": "output", "parameters": {
                "format_mode": "custom", "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "bg", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }
    return NodeGraph.from_dict(data)


def _reformat_graph() -> NodeGraph:
    data = {
        "nodes": [
            {"id": "bg", "type": "constant", "parameters": {
                "width": W, "height": H, "mode": "solid",
                "color": list(SOLID_COLOR)}},
            {"id": "rf", "type": "reformat", "parameters": {
                "format_mode": "custom", "width": W, "height": H}},
            {"id": "out", "type": "output", "parameters": {
                "format_mode": "custom", "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "bg", "src_port_name": "result",
             "dst_node_id": "rf", "dst_port_name": "image"},
            {"src_node_id": "rf", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }
    return NodeGraph.from_dict(data)


def _linear_to_srgb_arr(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92,
                    1.055 * np.power(np.maximum(x, 0.0), 1.0 / 2.4) - 0.055)


def _expected_bgr_u8() -> np.ndarray:
    lin = np.array(SOLID_COLOR[:3], dtype=np.float64)
    srgb = _linear_to_srgb_arr(lin)
    return np.clip(srgb * 255.0 + 0.5, 0, 255).astype(np.uint8)[::-1]


@pytest.fixture()
def headless_renderer(monkeypatch):
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
        ctx.release()
    except Exception as exc:  # pragma: no cover - CI GPU availability
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    renderer = CompositeExportRenderer()
    renderer.set_persistent_mode(True)
    yield renderer
    renderer.cleanup()


def _assert_solid_preview(image: np.ndarray) -> None:
    assert image.dtype == np.uint8
    assert image.shape == (H, W, 3)
    expected = _expected_bgr_u8()
    for y, x in ((8, 8), (H // 2, W // 2), (H - 9, W - 9), (H - 9, 8)):
        assert abs(int(image[y, x, 0]) - int(expected[0])) <= 2
        assert abs(int(image[y, x, 1]) - int(expected[1])) <= 2
        assert abs(int(image[y, x, 2]) - int(expected[2])) <= 2
    # Seam check: a solid input must stay uniform across tile boundaries.
    for ch in range(3):
        assert int(np.ptp(image[..., ch].astype(np.int32))) <= 2


class TestPreviewLargeFrameStrategy:
    def test_default_budget_uses_full_frame(self, headless_renderer):
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="srgb_u8")
        assert not result.error, result.error
        assert result.preview_strategy == "full_frame"
        _assert_solid_preview(result.image)

    def test_tight_budget_uses_gpu_stitch(self, monkeypatch,
                                          headless_renderer):
        # Make the full-frame transient estimate exceed any budget so the
        # decision falls through to the GPU-stitch tile path.
        monkeypatch.setattr(CompositeExportRenderer,
                            "FULL_FRAME_TRANSIENT_FACTOR", 10 ** 9)
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="srgb_u8")
        assert not result.error, result.error
        assert result.preview_strategy == "tiled_gpu_stitch"
        assert result.used_tiling and result.tile_count >= 2
        _assert_solid_preview(result.image)

    def test_reformat_inner_fallback_records_actual_path(self, monkeypatch,
                                                         headless_renderer):
        """P0-1 item 4: when the decision says GPU-stitch but the graph is
        tile-incompatible, the recorded strategy must describe the path
        actually taken (full_frame + reformat reason), not the decision."""
        monkeypatch.setattr(CompositeExportRenderer,
                            "FULL_FRAME_TRANSIENT_FACTOR", 10 ** 9)
        snapshot = _reformat_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="srgb_u8")
        assert not result.error, result.error
        assert result.preview_strategy == "full_frame"
        assert "reformat" in (result.preview_strategy_reason or "")
        _assert_solid_preview(result.image)

    def test_export_path_unchanged_linear_tiled(self, headless_renderer):
        snapshot = _solid_graph().snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="linear_f32")
        assert not result.error, result.error
        assert result.used_tiling
        assert result.image.dtype == np.float32
        assert result.image.shape == (H, W, 3)
        # linear f32 export keeps scene-linear values (no OETF)
        center = result.image[H // 2, W // 2]
        assert center[2] == pytest.approx(SOLID_COLOR[0], abs=0.01)  # R in BGR
        assert center[1] == pytest.approx(SOLID_COLOR[1], abs=0.01)
        assert center[0] == pytest.approx(SOLID_COLOR[2], abs=0.01)


class TestFrameInterpolateGpuFallback:
    """P2-9: the no-model fallback must blend on the GPU (no CPU
    round-trip) and stay numerically equivalent to the numpy mix."""

    def test_gpu_blend_matches_numpy_reference(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        try:
            from src.shared.gpu.gl_backend import FramebufferManager
            from src.features.composite.render.gpu_resource_pool import (
                GPUResourcePool,
            )
            from src.features.composite.nodes.frame_interpolate_node import (
                FrameInterpolateNode,
            )
            from src.features.composite.nodes.base_node import TextureResult

            fb = FramebufferManager(ctx)
            pool = GPUResourcePool(fb, max_memory_mb=512)

            h, w = 48, 64
            rng = np.random.default_rng(7)
            a_np = rng.random((h, w, 4), dtype=np.float32)
            b_np = rng.random((h, w, 4), dtype=np.float32)

            def _upload(arr):
                tex = fb.get_temp_texture(w, h, components=4, dtype="f4")
                tex.write(np.ascontiguousarray(np.flipud(arr)).tobytes())
                return TextureResult(
                    texture_id=tex.glo, width=w, height=h,
                    alpha_premultiplied=True, color_space="linear")

            ta, tb = _upload(a_np), _upload(b_np)
            node = FrameInterpolateNode(node_id="fi")
            node.parameters["t"] = 0.25

            out = node._gpu_linear_blend(ta, tb, 0.25, ctx, fb, pool)
            assert out is not None
            tex = next((t for t in fb._active_textures
                        if t.glo == out.texture_id), None)
            assert tex is not None
            fbo = fb.get_fbo(tex)
            fbo.use()
            ctx.viewport = (0, 0, w, h)
            raw = fbo.read(components=4, dtype="f4")
            got = np.flipud(np.frombuffer(
                raw, dtype=np.float32).reshape(h, w, 4)).copy()
            expected = a_np * (1.0 - 0.25) + b_np * 0.25
            assert np.allclose(got, expected, atol=1e-5)
            assert node.used_fallback is True
        finally:
            ctx.release()
