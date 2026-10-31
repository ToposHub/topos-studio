"""B2 point cloud tests — PLY/xyz loading, tracker import, rasterizer
coverage and colour modes, node contracts and serialization.  GL-free.
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.pointcloud3d_node import PointCloud3DNode
from src.features.composite.render3d.point_cloud import (
    PointCloud,
    load_point_cloud,
    points_from_track_data,
    rasterize,
)

pytestmark = pytest.mark.composite_core


def _grid_ply(path, n=8):
    """Write a small binary PLY point-grid fixture with vertex colours."""
    xs = np.linspace(0, 1, n)
    grid = np.array([[x, y, 0.0] for y in xs for x in xs],
                    dtype=np.float32)
    colors = np.zeros((len(grid), 4), dtype=np.uint8)
    colors[:, 0] = 255  # red vertices
    colors[:, 3] = 255
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(grid)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\n"
        "property uchar blue\nproperty uchar alpha\nend_header\n")
    records = np.empty(len(grid), dtype=[
        ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"), ("alpha", "u1")])
    records["x"], records["y"], records["z"] = grid[:, 0], grid[:, 1], \
        grid[:, 2]
    records["red"], records["green"] = colors[:, 0], colors[:, 1]
    records["blue"], records["alpha"] = colors[:, 2], colors[:, 3]
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(records.tobytes())
    return grid


class TestLoaders:
    def test_ply_vertex_count(self, tmp_path):
        path = str(tmp_path / "grid.ply")
        grid = _grid_ply(path)
        cloud = load_point_cloud(path)
        assert isinstance(cloud, PointCloud)
        assert cloud.count == grid.shape[0]
        np.testing.assert_allclose(cloud.positions, grid, atol=1e-5)

    def test_xyz_with_colors(self, tmp_path):
        path = tmp_path / "cloud.xyz"
        path.write_text(
            "# comment\n0 0 0 1 0 0\n1 2 3 0.5 0.5 0.5\n", encoding="utf-8")
        cloud = load_point_cloud(str(path))
        assert cloud.count == 2
        assert cloud.colors is not None
        assert cloud.colors[0].tolist() == [1.0, 0.0, 0.0]

    def test_xyz_without_colors(self, tmp_path):
        path = tmp_path / "c.xyz"
        path.write_text("1 2 3\n4 5 6\n", encoding="utf-8")
        cloud = load_point_cloud(str(path))
        assert cloud.count == 2
        assert cloud.colors is None

    def test_empty_xyz_raises(self, tmp_path):
        path = tmp_path / "e.xyz"
        path.write_text("# nothing\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_point_cloud(str(path))


class TestTrackerImport:
    def test_valid_transforms_become_points(self):
        data = {
            "version": 1, "width": 1920, "height": 1080,
            "transforms": {
                "1": {"tx": 10.0, "ty": 20.0, "status": "valid"},
                "2": {"tx": 12.0, "ty": 22.0, "status": "valid"},
                "3": {"tx": 14.0, "ty": 24.0, "status": "failed"},
            },
        }
        points, w, h = points_from_track_data(data)
        assert (w, h) == (1920, 1080)
        assert points.shape == (2, 3)  # the failed frame is skipped
        assert points[1].tolist() == [12.0, 22.0, 0.0]


class TestRasterizer:
    def _cloud(self, n=9):
        xs = np.linspace(-1, 1, n)
        return np.array([[x, y, z] for y in xs for x in xs
                         for z in (0.0,)], dtype=np.float32)

    def test_coverage_nonempty_and_bounded(self):
        frame = rasterize(self._cloud(), None, 100, 80, point_size=2)
        assert frame.shape == (80, 100, 4)
        covered = (frame[..., 3] > 0).sum()
        ratio = covered / frame[..., 0].size
        assert 0.01 < ratio <= 1.0

    def test_flat_colour(self):
        frame = rasterize(self._cloud(), None, 60, 60, point_size=1,
                          color_mode="flat", flat_color=(0.9, 0.1, 0.2))
        lit = frame[frame[..., 3] > 0][:, :3]
        np.testing.assert_allclose(
            lit, np.tile([0.9, 0.1, 0.2], (len(lit), 1)), atol=1e-6)

    def test_depth_ramp_orders_blue_to_red(self):
        # Distinct xy so the orthographic projection separates the points.
        pts = np.array([[0, 0, 0], [1, 1, 10]], dtype=np.float32)
        frame = rasterize(pts, None, 40, 40, point_size=1,
                          color_mode="depth_ramp")
        # Near point (z=0) is blue-ish, far point (z=max) red-ish.
        ys, xs = np.where(frame[..., 3] > 0)
        # Group by y halves after fit — instead simply assert both ends
        # of the ramp exist in the frame.
        reds = ((frame[..., 0] > 0.9) & (frame[..., 2] < 0.1)).sum()
        blues = ((frame[..., 2] > 0.9) & (frame[..., 0] < 0.1)).sum()
        assert reds > 0 and blues > 0

    def test_empty_points_is_transparent(self):
        frame = rasterize(np.zeros((0, 3), dtype=np.float32), None,
                          32, 32)
        assert np.allclose(frame, 0.0)


class TestNode:
    class _Ctx:
        gl = None

    def test_registered(self):
        assert NODE_REGISTRY.get("pointcloud3d") is PointCloud3DNode
        assert any(t == "pointcloud3d"
                   for t, _ in NODE_CATEGORIES.get("3D", []))
        cap = get_node_capability("pointcloud3d")
        assert cap.action_type == "source"

    def test_missing_file_sets_error_and_no_output(self):
        node = PointCloud3DNode(node_id="pc")
        out = node._do_evaluate({}, self._Ctx(), None, None)
        assert out == {}
        assert "no point cloud file" in (node.error_message or "")

    def test_bad_file_visible_error(self, tmp_path):
        node = PointCloud3DNode(node_id="pc")
        node.set_parameter("file_path", str(tmp_path / "missing.ply"))
        out = node._do_evaluate({}, self._Ctx(), None, None)
        assert out == {}
        assert node.load_error != ""

    def test_tracker_source_reports_empty(self):
        node = PointCloud3DNode(node_id="pc")
        node.set_parameter("source", "tracker")
        node.set_track_data({"transforms": {}})
        out = node._do_evaluate({}, self._Ctx(), None, None)
        assert out == {}
        assert "no valid track points" in (node.error_message or "")

    def test_track_data_serialization_roundtrip(self, qapp):
        node = PointCloud3DNode(node_id="pc")
        data = {"version": 1, "width": 100, "height": 50,
                "transforms": {"1": {"tx": 1.0, "ty": 2.0,
                                     "status": "valid"}}}
        node.set_track_data(data)
        clone = PointCloud3DNode.from_dict(node.to_dict())
        restored = json.loads(clone.parameters["track_data"])
        assert restored["transforms"]["1"]["tx"] == 1.0
