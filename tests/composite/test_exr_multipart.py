"""CP-068 multi-part EXR writer tests (roundtrip via project reader)."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core

openexr = pytest.importorskip("OpenEXR", reason="OpenEXR bindings unavailable")


def _beauty(h=4, w=6):
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    rgba[..., 0] = np.linspace(0.1, 0.9, w)[None, :]
    rgba[..., 1] = 0.2
    rgba[..., 2] = 0.3
    rgba[..., 3] = 1.0
    return rgba


class TestMultipartWriter:
    def test_roundtrip_beauty_plus_aovs(self, tmp_path):
        from src.features.composite.core.exr_multipart_writer import (
            write_exr_multipart,
        )
        h, w = 4, 6
        beauty = _beauty(h, w)
        depth = np.full((h, w), 7.5, dtype=np.float32)
        normal = np.zeros((h, w, 3), dtype=np.float32)
        normal[..., 2] = 1.0
        out = str(tmp_path / "multi.exr")
        assert write_exr_multipart(out, beauty, {
            "depth": depth, "normal": normal})

        f = openexr.File(out, separate_channels=True)
        names = [p.name() for p in f.parts]
        assert "beauty" in names and "depth" in names and "normal" in names
        depth_part = next(p for p in f.parts if p.name() == "depth")
        z = np.asarray(depth_part.channels["Z"].pixels)
        assert z.shape == (h, w)
        assert np.allclose(z, 7.5, atol=1e-5)
        beauty_part = next(p for p in f.parts if p.name() == "beauty")
        r = np.asarray(beauty_part.channels["R"].pixels)
        assert np.allclose(r, beauty[..., 0], atol=1e-5)

    def test_project_reader_sees_parts(self, tmp_path):
        from src.features.composite.core.exr_multipart_writer import (
            write_exr_multipart,
        )
        from src.features.composite.core.image_loader import probe_exr_header
        out = str(tmp_path / "probe.exr")
        assert write_exr_multipart(out, _beauty(),
                                   {"depth": np.full((4, 6), 2.0, np.float32)})
        header = probe_exr_header(out)
        assert header is not None
        parts = header.get("parts") or []
        names = [p.get("name") for p in parts]
        assert "beauty" in names and "depth" in names

    def test_size_mismatched_pass_skipped(self, tmp_path):
        from src.features.composite.core.exr_multipart_writer import (
            write_exr_multipart,
        )
        out = str(tmp_path / "mismatch.exr")
        ok = write_exr_multipart(out, _beauty(4, 6), {
            "depth": np.full((8, 6), 1.0, np.float32)})  # 高度不符
        assert ok  # beauty 仍写出
        f = openexr.File(out)
        assert [p.name() for p in f.parts] == ["beauty"]

    def test_node_exposes_multipart_param(self):
        from src.features.composite.nodes.file_output_node import FileOutputNode
        node = FileOutputNode(node_id="fo")
        assert "exr_multipart" in node.parameters
        pdefs = {p.name for p in node.PARAM_DEFS}
        assert "exr_multipart" in pdefs


class TestFullCompressionEnum:
    """CP-006: 绑定路由的完整压缩枚举。"""

    @pytest.mark.parametrize("comp", ["ZIP", "ZIPS", "PIZ", "PXR24", "DWAB"])
    def test_binding_compressions_write(self, tmp_path, comp):
        import OpenEXR
        from src.features.composite.core.exr_multipart_writer import (
            write_exr_multipart,
        )
        out = str(tmp_path / f"c_{comp}.exr")
        assert write_exr_multipart(out, _beauty(), {}, compression=comp)
        f = OpenEXR.File(out)
        assert f.parts[0].name() == "beauty"

    def test_node_compression_labels_extended(self):
        from src.features.composite.nodes.file_output_node import FileOutputNode
        labels = next(p.labels for p in FileOutputNode.PARAM_DEFS
                      if p.name == "compression")
        for comp in ("zip", "zips", "piz", "pxr24", "dwaa", "dwab"):
            assert comp in labels
