"""B1 volume render tests (downgraded path) — npz dense volumes with
analytic Beer–Lambert verification, the pyopenvdb dependency gate, safe
degradation, and node contracts.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.volume3d_node import Volume3DNode
from src.features.composite.render3d.volume_grid import (
    VolumeDependencyError,
    integrate_transmittance,
    load_volume,
    save_volume,
)

pytestmark = pytest.mark.composite_core


class _Ctx:
    gl = None


class TestVolumeGrid:
    def test_npz_roundtrip(self, tmp_path):
        density = np.random.default_rng(0).random(
            (8, 16, 12)).astype(np.float32)
        path = str(tmp_path / "vol.npz")
        save_volume(path, density, spacing=0.5)
        grid = load_volume(path)
        assert grid.shape == (8, 16, 12)
        np.testing.assert_allclose(grid.density, density, atol=1e-6)

    def test_negative_density_clamped(self):
        from src.features.composite.render3d.volume_grid import VolumeGrid
        grid = VolumeGrid(np.array([[[-1.0, 2.0]]], dtype=np.float32))
        assert float(grid.density.min()) == 0.0

    def test_bad_shape_rejected(self):
        from src.features.composite.render3d.volume_grid import VolumeGrid
        with pytest.raises(ValueError):
            VolumeGrid(np.zeros((4, 4), dtype=np.float32))

    def test_vdb_hits_dependency_gate(self, tmp_path):
        path = tmp_path / "smoke.vdb"
        path.write_bytes(b"VDB-headervdb")
        with pytest.raises(VolumeDependencyError) as exc_info:
            load_volume(str(path))
        assert "pyopenvdb" in str(exc_info.value)

    def test_unknown_format_rejected(self, tmp_path):
        path = tmp_path / "vol.bin"
        path.write_bytes(b"x")
        with pytest.raises(ValueError):
            load_volume(str(path))


class TestAnalytic:
    def test_uniform_density_beer_lambert(self):
        # σ = 0.5, length 4.0 (8 samples × 0.5) → T = exp(-2)
        line = np.full(8, 0.5, dtype=np.float32)
        t = integrate_transmittance(line, step_world=0.5)
        assert t == pytest.approx(float(np.exp(-2.0)), rel=1e-5)

    def test_zero_density_is_transparent(self):
        line = np.zeros(8, dtype=np.float32)
        assert integrate_transmittance(line, 0.5) == 1.0


class TestNode:
    def test_registered(self):
        assert NODE_REGISTRY.get("volume3d") is Volume3DNode
        assert any(t == "volume3d"
                   for t, _ in NODE_CATEGORIES.get("3D", []))
        cap = get_node_capability("volume3d")
        assert cap.action_type == "source"

    def test_no_file_sets_error(self):
        node = Volume3DNode(node_id="v")
        assert node._do_evaluate({}, _Ctx(), None, None) == {}
        assert "no file" in (node.error_message or "")

    def test_vdb_shows_dependency_error(self, tmp_path):
        node = Volume3DNode(node_id="v")
        path = tmp_path / "s.vdb"
        path.write_bytes(b"VDB")
        node.set_parameter("file_path", str(path))
        out = node._do_evaluate({}, _Ctx(), None, None)
        assert out == {}
        assert "pyopenvdb" in (node.error_message or "")

    def test_serialization_roundtrip(self, qapp):
        node = Volume3DNode(node_id="v")
        node.set_parameter("density_multiplier", 2.5)
        node.set_parameter("step_count", 64)
        clone = Volume3DNode.from_dict(node.to_dict())
        assert clone.parameters["density_multiplier"] == pytest.approx(2.5)
        assert clone.parameters["step_count"] == 64
