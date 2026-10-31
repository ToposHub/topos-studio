"""Regression tests for composite-node audit fixes (batch A round 2).

Covers:
  * AUD-6803 — ``Geo3DNode``: rebuilding the instance VBO must invalidate
    the cached VAO (it still bound the released buffer), otherwise
    instanced rendering reads instance attributes out of bounds of a
    deleted buffer when only grid_x/grid_z/spacing changed.
  * AUD-7110 — ``PointCloud3DNode``: the GPU texture is self-managed
    (created once per content signature, released on rebuild and in
    ``cleanup()``) instead of leaking one texture per evaluation.
  * AUD-7504 — ``TransformNode._build_inverse_matrix`` must be the true
    inverse of the forward affine (forward ∘ inverse == identity for
    rotation != 0, non-zero pivot and non-zero translate).
  * AUD-7601 — ``VideoFrameNode._frame_cache`` entries must not alias the
    shared conversion scratch buffers (``_rgba_buffer`` /
    ``_bg_rgba_buffer``): cache hits and present-latest otherwise upload
    the most recently decoded neighbour frame.
  * AUD-7701 — ``Volume3DNode`` uploads premultiplied RGB
    (absorption × alpha) and must declare ``alpha_premultiplied=True``.
"""

import sys
import os
import inspect

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.nodes.geo3d_node import Geo3DNode
from src.features.composite.nodes.pointcloud3d_node import PointCloud3DNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.volume3d_node import Volume3DNode

pytestmark = pytest.mark.composite_core


# ── Fakes ─────────────────────────────────────────────────────────────────


class _FakeBuffer:
    def __init__(self, data: bytes = b"") -> None:
        self.data = bytes(data)
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeProgram:
    def release(self) -> None:
        pass


class _FakeVAO:
    def __init__(self, program, content, index_buffer=None) -> None:
        self.program = program
        self.content = list(content)  # [(buffer, fmt, names...), ...]
        self.index_buffer = index_buffer
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeGL:
    """Minimal ModernGL stand-in for buffer/program/vertex_array."""

    def buffer(self, data: bytes = b"") -> _FakeBuffer:
        return _FakeBuffer(data)

    def program(self, vertex_shader: str = "", fragment_shader: str = ""
                ) -> _FakeProgram:
        return _FakeProgram()

    def vertex_array(self, program, content, index_buffer=None) -> _FakeVAO:
        return _FakeVAO(program, content, index_buffer)


class _FakeTexture:
    def __init__(self, size, components, data=None, dtype="f4") -> None:
        self.size = size
        self.components = components
        self.data = bytes(data or b"")
        self.dtype = dtype
        self.glo = id(self)
        self.released = False

    def release(self) -> None:
        self.released = True


class _TextureTrackingGL(_FakeGL):
    """GL fake that records every texture() allocation."""

    def __init__(self) -> None:
        super().__init__()
        self.textures: list = []

    def texture(self, size, components, data=None, dtype="f4") -> _FakeTexture:
        tex = _FakeTexture(size, components, data, dtype)
        self.textures.append(tex)
        return tex


def _apply_matrix(matrix, x: float, y: float):
    """Apply a (m00, m10, m01, m11, m02, m12) affine to a point."""
    m00, m10, m01, m11, m02, m12 = matrix
    return (m00 * x + m01 * y + m02, m10 * x + m11 * y + m12)


# ── AUD-6803: Geo3D instance VBO rebuild must invalidate the VAO ──────────


class TestAud6803Geo3dVaoInvalidation:
    """Instance signature change (primitive/instance_enabled untouched)
    must rebuild the VAO — never render a VAO bound to a released VBO."""

    def _build(self, gl, grid_x, grid_z, spacing):
        node = Geo3DNode("geo")
        count = node._ensure_instance_buffer(gl, grid_x, grid_z, spacing)
        node._ensure_mesh_geometry(gl, "cube", True)
        return node, count

    def test_instance_sig_change_releases_stale_vao_and_vbo(self):
        gl = _FakeGL()
        node, count1 = self._build(gl, 3, 3, 2.0)
        assert count1 == 9
        vao1 = node._mesh_vao
        vbo1 = node._instance_vbo
        assert vao1 is not None and not vao1.released

        # grid_x 3→5: primitive and instance_enabled are unchanged, only the
        # instance signature changes — the AUD-6803 trigger.
        count2 = node._ensure_instance_buffer(gl, 5, 3, 2.0)
        assert count2 == 15
        assert vbo1.released, "old instance VBO must be released"
        assert vao1.released, (
            "cached VAO still referenced the released instance VBO — "
            "AUD-6803: rendering it by the new instance count reads "
            "attributes out of bounds of a deleted buffer")
        assert node._mesh_vao is not vao1

        # Follow-up geometry ensure must rebuild a live VAO bound to the
        # NEW instance VBO (the _do_evaluate call order).
        node._ensure_mesh_geometry(gl, "cube", True)
        vao2 = node._mesh_vao
        assert vao2 is not vao1 and not vao2.released
        instance_bindings = [c for c in vao2.content if "/i" in str(c[1])]
        assert instance_bindings, "instanced VAO must carry the offset buffer"
        assert instance_bindings[0][0] is node._instance_vbo
        assert node._instance_vbo is not vbo1

    def test_unchanged_instance_sig_keeps_vao_cached(self):
        gl = _FakeGL()
        node, _ = self._build(gl, 3, 3, 2.0)
        vao1 = node._mesh_vao
        vbo1 = node._instance_vbo
        # Same signature: buffer and VAO are reused, nothing released.
        assert node._ensure_instance_buffer(gl, 3, 3, 2.0) == 9
        node._ensure_mesh_geometry(gl, "cube", True)
        assert node._mesh_vao is vao1
        assert node._instance_vbo is vbo1
        assert not vao1.released and not vbo1.released

    def test_fix_present_in_source(self):
        """Instance buffer rebuild path invalidates the cached VAO."""
        src = inspect.getsource(Geo3DNode._ensure_instance_buffer)
        assert "_mesh_vao.release()" in src
        assert "_last_primitive = None" in src


# ── AUD-7110: PointCloud3D self-managed GPU texture ───────────────────────


def _track_data_json() -> str:
    import json
    return json.dumps({
        "width": 640, "height": 480,
        "transforms": {
            "0": {"tx": 10.0, "ty": 20.0, "status": "valid"},
            "1": {"tx": 200.0, "ty": 100.0, "status": "valid"},
            "2": {"tx": 300.0, "ty": 250.0, "status": "valid"},
        },
    })


class TestAud7110PointcloudTextureLifecycle:
    def _evaluate(self, node, gl):
        return node._do_evaluate({}, gl, None)

    def _make_node(self) -> PointCloud3DNode:
        node = PointCloud3DNode("pc")
        node.set_parameter("source", "tracker")
        node.set_parameter("track_data", _track_data_json())
        return node

    def test_texture_reused_across_evaluations(self):
        gl = _TextureTrackingGL()
        node = self._make_node()
        r1 = self._evaluate(node, gl)["result"]
        assert len(gl.textures) == 1
        r2 = self._evaluate(node, gl)["result"]
        # Same content signature → the self-managed texture is reused, not
        # re-allocated (the leak: one w×h RGBA32F texture per evaluation).
        assert len(gl.textures) == 1
        assert r2.texture_id == r1.texture_id
        assert not gl.textures[0].released
        assert r2.revision == r1.revision

    def test_content_change_releases_old_texture_and_bumps_revision(self):
        gl = _TextureTrackingGL()
        node = self._make_node()
        r1 = self._evaluate(node, gl)["result"]
        node.set_parameter("point_size", 5)  # animated param, affects pixels
        r2 = self._evaluate(node, gl)["result"]
        assert len(gl.textures) == 2
        assert gl.textures[0].released, "stale texture must be released"
        assert not gl.textures[1].released
        assert r2.texture_id != r1.texture_id
        assert r2.revision > r1.revision
        assert node._cache_invalidated_needed is True

    def test_cleanup_releases_texture(self):
        gl = _TextureTrackingGL()
        node = self._make_node()
        self._evaluate(node, gl)
        node.cleanup()
        assert gl.textures[0].released
        assert node._gpu_texture is None

    def test_result_metadata_contract(self):
        gl = _TextureTrackingGL()
        node = self._make_node()
        r = self._evaluate(node, gl)["result"]
        # particle/text source-node contract: revision + format + bbox, and
        # the rasterizer writes straight RGB (alpha only on point pixels).
        assert r.revision >= 1
        assert r.format is not None and r.format.width == 640
        assert r.bbox == BBox.full_frame(640, 480)
        assert r.alpha_premultiplied is False


# ── AUD-7504: Transform inverse matrix ────────────────────────────────────


class TestAud7504TransformInverseMatrix:
    CASES = [
        # (params, W, H)
        ({"rotation": 0.0}, 64, 48),                                   # identity-ish
        ({"translate_x": 10.0, "translate_y": -7.5}, 64, 48),          # pure translate
        ({"scale_x": 1.5, "scale_y": 0.8}, 64, 48),                    # pure scale
        ({"rotation": 90.0}, 64, 48),                                  # pure rotation
        ({"rotation": -37.0}, 100, 40),                                # negative rotation
        ({"rotation": 30.0, "scale_x": 1.5, "scale_y": 0.8,
          "translate_x": 12.0, "translate_y": -4.0}, 96, 64),          # everything
        ({"rotation": 45.0, "pivot_mode": "custom", "pivot_x": 2.0,
          "pivot_y": 3.0, "translate_x": 10.0}, 32, 24),               # audit case
        ({"rotation": 90.0, "pivot_mode": "custom", "pivot_x": 2.0,
          "pivot_y": 3.0, "translate_x": 10.0}, 32, 24),               # audit numeric
    ]

    @pytest.mark.parametrize("params,W,H", CASES)
    def test_inverse_is_true_inverse(self, params, W, H):
        node = TransformNode("t")
        fwd = node._build_transform_matrix(params, W, H)
        inv = node._build_inverse_matrix(params, W, H)
        pts = [(0.0, 0.0), (5.0, 7.0), (W / 2.0, H / 2.0),
               (float(W - 1), float(H - 1)), (-3.5, 2.25), (W * 1.3, -H * 0.2)]
        for (x, y) in pts:
            fwd_pt = _apply_matrix(fwd, x, y)
            back_pt = _apply_matrix(inv, *fwd_pt)
            assert back_pt == pytest.approx((x, y), abs=1e-9), (
                f"forward∘inverse != identity at {(x, y)} for {params}")
            inv_pt = _apply_matrix(inv, x, y)
            back2 = _apply_matrix(fwd, *inv_pt)
            assert back2 == pytest.approx((x, y), abs=1e-9), (
                f"inverse∘forward != identity at {(x, y)} for {params}")

    def test_audit_numeric_regression(self):
        """Audit case: rot=90°, pivot (2,3), tx=10 — (5,7) → forward (8,6)
        → the fixed inverse must recover (5,7) (old code gave (-1,-1))."""
        params = {"rotation": 90.0, "pivot_mode": "custom",
                  "pivot_x": 2.0, "pivot_y": 3.0, "translate_x": 10.0}
        node = TransformNode("t")
        fwd = node._build_transform_matrix(params, 32, 24)
        inv = node._build_inverse_matrix(params, 32, 24)
        fwd_pt = _apply_matrix(fwd, 5.0, 7.0)
        assert fwd_pt == pytest.approx((8.0, 6.0), abs=1e-9)
        back_pt = _apply_matrix(inv, *fwd_pt)
        assert back_pt == pytest.approx((5.0, 7.0), abs=1e-9)

    def test_compute_input_roi_covers_forward_content(self):
        """ROI property: ``compute_input_roi`` of the output bbox must
        recover a region that still contains the whole input bbox (the
        shader must sample at least those pixels). With the old wrong
        inverse (rotation applied forward twice) the recovered region was
        rotated/scaled the wrong way and missed the input corners."""
        node = TransformNode("t")
        node.set_parameter("rotation", 33.0)
        node.set_parameter("scale_x", 1.4)
        node.set_parameter("scale_y", 0.75)
        node.set_parameter("translate_x", 9.0)
        node.set_parameter("translate_y", -5.0)
        in_box = BBox(x=3, y=5, width=40, height=30)
        out_box = node.compute_output_bbox({"image": in_box}, node.parameters)
        assert out_box is not None and not out_box.is_empty
        roi = node.compute_input_roi(out_box, node.parameters,
                                     {"image": in_box})["image"]
        # Both matrices use the same Y-down convention flipped around
        # in_box.height (see _transform_bbox); roi comes back in the same
        # Y-up space as in_box, so the containment check is direct.
        for (cx, cy) in [(in_box.x, in_box.y), (in_box.right, in_box.y),
                         (in_box.x, in_box.top), (in_box.right, in_box.top)]:
            assert roi.x - 1e-6 <= cx <= roi.right + 1e-6, (
                f"input corner {(cx, cy)} outside recovered ROI {roi}")
            assert roi.y - 1e-6 <= cy <= roi.top + 1e-6, (
                f"input corner {(cx, cy)} outside recovered ROI {roi}")


# ── AUD-7601: VideoFrame cache entries must not alias scratch ─────────────


class TestAud7601VideoFrameCacheDealias:
    def test_own_cache_frame_data_copies_scratch_alias(self):
        node = VideoFrameNode("vf")
        scratch = np.full((4, 6, 4), 0.5, dtype=np.float32)
        node._rgba_buffer = scratch
        owned = node._own_cache_frame_data(scratch)
        assert owned is not scratch
        np.testing.assert_array_equal(owned, scratch)

    def test_own_cache_frame_data_passthrough_for_owned_arrays(self):
        node = VideoFrameNode("vf")
        node._rgba_buffer = np.zeros((4, 6, 4), dtype=np.float32)
        node._bg_rgba_buffer = np.zeros((4, 6, 4), dtype=np.float32)
        independent = np.full((4, 6, 4), 1.0, dtype=np.float32)
        assert node._own_cache_frame_data(independent) is independent
        # Non-ndarray entries (planar/VT) pass through untouched.
        marker = object()
        assert node._own_cache_frame_data(marker) is marker

    def test_frame_cache_entries_survive_next_decode(self):
        """Two sequential background decodes share _bg_rgba_buffer; each
        cached frame must own its pixels (old code: cache[0] content became
        the last decoded frame — wrong frame on cache hit / present-latest,
        and short-loop exports rendered the loop's final frame twice)."""
        node = VideoFrameNode("vf")
        node._bg_rgba_buffer = np.zeros((4, 6, 4), dtype=np.float32)

        def fake_decode(frame_number, render_mode, *, background=False,
                        scrub=None, sync_vt=False):
            buf = node._bg_rgba_buffer
            buf[:] = float(frame_number)  # "content" = frame number
            return buf  # aliasing scratch, exactly like the real converter

        node._decode_frame = fake_decode
        with node._cache_lock:
            generation = node._media_generation
        assert node._decode_and_store(0, "preview", generation,
                                      notify=False) is True
        assert node._decode_and_store(1, "preview", generation,
                                      notify=False) is True
        c0 = node._frame_cache[0]
        c1 = node._frame_cache[1]
        assert c0 is not c1, "cache entries aliased the same scratch buffer"
        assert c0 is not node._bg_rgba_buffer
        assert c1 is not node._bg_rgba_buffer
        # Each entry keeps its own frame's content after the next decode
        # overwrote the scratch.
        assert c0.flat[0] == pytest.approx(0.0)
        assert c1.flat[0] == pytest.approx(1.0)


# ── AUD-7701: Volume3D premultiplied flag ─────────────────────────────────


class TestAud7701VolumePremultipliedFlag:
    def _make_node(self, tmp_path) -> Volume3DNode:
        from src.features.composite.render3d.volume_grid import save_volume
        density = np.full((8, 8, 8), 0.25, dtype=np.float32)
        path = str(tmp_path / "vol.npz")
        save_volume(path, density, spacing=0.5)
        node = Volume3DNode("vol")
        node.set_parameter("file_path", path)
        node.set_parameter("output_width", 16)
        node.set_parameter("output_height", 12)
        return node

    def test_result_declares_premultiplied(self, tmp_path):
        gl = _TextureTrackingGL()
        node = self._make_node(tmp_path)
        out = node._do_evaluate({}, gl, None)
        assert "result" in out
        result = out["result"]
        # RGB = absorption × alpha is premultiplied data. Declaring
        # alpha_premultiplied=False made downstream Merge multiply alpha
        # in a second time (absorption × alpha² — volume darkened).
        assert result.alpha_premultiplied is True

    def test_uploaded_pixels_are_premultiplied(self, tmp_path):
        gl = _TextureTrackingGL()
        node = self._make_node(tmp_path)
        result = node._do_evaluate({}, gl, None)["result"]
        assert len(gl.textures) == 1
        tex = gl.textures[0]
        img = np.frombuffer(tex.data, dtype=np.float32).reshape(
            tex.size[1], tex.size[0], 4)
        alpha = img[..., 3]
        # Uniform slab: alpha = 1 - T is constant and strictly inside (0,1),
        # and with the default white absorption colour the premultiplied
        # form means RGB == alpha exactly (data matches the flag).
        assert np.all(alpha > 0.0) and np.all(alpha < 1.0)
        for c in range(3):
            np.testing.assert_allclose(img[..., c], alpha, atol=1e-6)
        assert result.alpha_premultiplied is True
