"""Node thumbnails on Metal (plan 4.6).

A thumbnail is the output of one node's own cone of the graph -- what the
GL evaluator produces with ``viewer:<node_id>`` -- rendered through the
shared entry and encoded for display once.  Real device for the pictures;
the cache identity and the display encoding need none.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_entry import SharedColorGraphEntry
from src.features.color.core.frame_color_description import ColorFrameDescription
from src.features.color.core.layer import build_curves_lut_from_data
from src.features.color.core.metal_node_thumbnail import (
    ThumbnailCacheRecord,
    MetalNodeThumbnailService,
    display_encode_thumbnail,
    working_color_identity,
)
from src.shared.color.color_transform_provider import _linear_to_srgb_np, _tonemap_aces_np
from src.shared.gpu.metal.context import MetalComputeContext
from tests.color.fixtures.color_graph import _neutral_layer, f01_hdr_ramp
from tests.color.test_color_graph_migration import _stack_with_layers

pytest.importorskip("Metal")


def _sdr() -> ColorFrameDescription:
    return ColorFrameDescription("rgba16f", 16, 16, bit_depth=16)


class TestCacheIdentity:
    def test_the_key_carries_node_revision_resources_source_size_backend_and_colour(self) -> None:
        record = ThumbnailCacheRecord(
            node_id="n1", graph_revision=7, resource_fingerprint="fa", source_identity="clip-a",
            size=(96, 54), backend="metal_texture", working_color="signal:bt709")
        assert record.key() == (
            "n1", 7, "fa", "clip-a", (96, 54), "metal_texture", "signal:bt709")

    def test_a_record_from_the_service_changes_with_everything_that_changes_the_picture(self) -> None:
        service = MetalNodeThumbnailService(entry=None)
        base = service.cache_record(
            "n1", ("g", "clip-a", 7), "fa", "clip-a", (96, 54), "metal_texture", _sdr(), "signal")
        again = service.cache_record(
            "n1", ("g", "clip-a", 7), "fa", "clip-a", (96, 54), "metal_texture", _sdr(), "signal")
        assert base == again
        # another LUT or another upstream node -> another resource fingerprint
        assert base != service.cache_record(
            "n1", ("g", "clip-a", 7), "fb", "clip-a", (96, 54), "metal_texture", _sdr(), "signal")
        # undo -> another graph revision
        assert base != service.cache_record(
            "n1", ("g", "clip-a", 8), "fa", "clip-a", (96, 54), "metal_texture", _sdr(), "signal")
        # another source frame, another size
        assert base != service.cache_record(
            "n1", ("g", "clip-a", 7), "fa", "clip-a#120", (96, 54), "metal_texture", _sdr(), "signal")
        assert base != service.cache_record(
            "n1", ("g", "clip-a", 7), "fa", "clip-a", (48, 27), "metal_texture", _sdr(), "signal")
        # the compatible CPU graph and the Metal graph do not share a thumbnail
        assert base != service.cache_record(
            "n1", ("g", "clip-a", 7), "fa", "clip-a", (96, 54), "cpu_compat", _sdr(), "signal")
        # nor do two working colour semantics
        assert base != service.cache_record(
            "n1", ("g", "clip-a", 7), "fa", "clip-a", (96, 54), "metal_texture", _sdr(), "linear")

    def test_the_working_colour_identity_names_mode_transfer_and_primaries(self) -> None:
        sdr = _sdr()
        hdr = ColorFrameDescription("rgba16f", 16, 16, bit_depth=16, transfer="pq",
                                    primaries="bt2020")
        assert working_color_identity(sdr, "signal") == f"signal:{sdr.transfer}:{sdr.primaries}"
        assert working_color_identity(hdr, "linear") == "linear:pq:bt2020"


class TestDisplayEncoding:
    def test_a_signal_working_mode_passes_through_clipped(self) -> None:
        image = np.full((2, 3, 4), [0.25, 2.0, -0.5, 1.0], np.float32)
        out = display_encode_thumbnail(image, _sdr(), "signal")
        assert out.shape == (2, 3, 3) and out.dtype == np.float32
        assert np.all(out == np.asarray([0.25, 1.0, 0.0], np.float32))

    def test_linear_sdr_is_the_srgb_curve(self) -> None:
        image = np.linspace(0.0, 1.0, 12, dtype=np.float32).reshape(1, 12, 1) * np.ones((1, 1, 4), np.float32)
        out = display_encode_thumbnail(image, _sdr(), "linear")
        assert np.allclose(out, np.clip(_linear_to_srgb_np(image[..., :3]), 0.0, 1.0), atol=1e-6)

    def test_linear_hdr_is_tone_mapped_to_sdr(self) -> None:
        hdr = ColorFrameDescription("rgba16f", 16, 16, bit_depth=16, transfer="pq", primaries="bt2020")
        assert hdr.is_hdr
        image = np.full((2, 2, 4), [4.0, 0.5, 0.1, 1.0], np.float32)
        out = display_encode_thumbnail(image, hdr, "linear")
        assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0
        expected = np.clip(_linear_to_srgb_np(_tonemap_aces_np(image[..., :3], target_peak=100.0)), 0.0, 1.0)
        assert np.allclose(out, expected, atol=1e-6)


def _graph():
    """Source -> c1 (curves) -> c2 (exposure) -> output."""
    first = _neutral_layer("c1")
    first.curves_data = {"M": [(0, 255), (128, 40), (255, 0)]}
    first.curves_lut = build_curves_lut_from_data(first.curves_data)
    second = _neutral_layer("c2")
    second.grading_params.update({"exposure": 25.0, "saturation": -50.0})
    return __import__("src.features.color.core.color_graph_migration", fromlist=["migrate_legacy_stack"]) \
        .migrate_legacy_stack(_stack_with_layers([first, second]))


def _correctors(graph) -> list:
    return [n.node_id for n in graph.nodes if n.node_type == "corrector"]


@pytest.fixture(scope="module")
def entry():
    if not MetalComputeContext.is_supported():
        pytest.skip(f"no Metal device: {MetalComputeContext.availability_reason()[1]}")
    return SharedColorGraphEntry(MetalComputeContext())


@pytest.fixture()
def service(entry) -> MetalNodeThumbnailService:
    return MetalNodeThumbnailService(entry)


class TestThumbnails:
    def test_each_node_gets_its_own_cone_as_an_opaque_rgba8_picture(self, entry, service) -> None:
        graph = _graph()
        first, second = _correctors(graph)
        source = f01_hdr_ramp(16)
        results = service.evaluate(graph, source, (first, second))
        for node_id in (first, second):
            picture, backend = results[node_id]
            assert backend == "metal_texture"
            assert picture.dtype == np.uint8 and picture.shape == (16, 16, 4)
            assert np.all(picture[..., 3] == 255)
        # The second node sees the first one's grade and adds its own.
        assert not np.array_equal(results[first][0], results[second][0])
        assert service.stats == {"evaluated": 2, "failed": 0, "fallback_cpu": 0}

    def test_a_thumbnail_is_the_picture_of_the_graph_cut_after_that_node(self, entry, service) -> None:
        """``viewer:<node>`` compiles that node's cone: the same node with the
        same grade, as the output of the graph cut right after it."""
        from dataclasses import replace

        from src.shared.project.color_graph import ColorEdgeData, NodeType

        graph = _graph()
        first, _second = _correctors(graph)
        source = f01_hdr_ramp(16)
        (thumbnail, backend), = service.evaluate(graph, source, (first,)).values()
        assert backend == "metal_texture"

        by_type = lambda kind: next(n for n in graph.nodes if n.node_type == kind)  # noqa: E731
        source_node, output_node = by_type(NodeType.SOURCE.value), by_type(NodeType.OUTPUT.value)
        cut = replace(
            graph, nodes=(source_node, by_type_first := next(n for n in graph.nodes if n.node_id == first), output_node),
            edges=(ColorEdgeData("cut-1", source_node.node_id, "image", by_type_first.node_id, "image"),
                   ColorEdgeData("cut-2", by_type_first.node_id, "image", output_node.node_id, "image")))
        whole = entry.render_frame(cut, 0, source, media_id="cut")
        assert whole.published and whole.backend == "metal_texture"
        encoded = (display_encode_thumbnail(whole.output, _sdr(), "signal") * 255.0 + 0.5).astype(np.uint8)
        assert np.array_equal(thumbnail[..., :3], encoded)

    def test_a_node_that_is_not_in_the_graph_gets_no_picture(self, service) -> None:
        results = service.evaluate(_graph(), f01_hdr_ramp(16), ("no-such-node",))
        picture, reason = results["no-such-node"]
        assert picture is None and reason              # a diagnostic, never the source
        assert service.stats["failed"] == 1

    def test_a_refused_domain_is_not_replaced_by_the_source(self, service) -> None:
        from dataclasses import replace

        from src.shared.project.color_graph import NodeType

        graph = _graph()
        node = next(n for n in graph.nodes if n.node_type == NodeType.CORRECTOR.value)
        payload = {**dict(node.legacy_payload or {}),
                   "plugins": [{"name": "nope", "plugin_id": "x", "instance_id": "i"}]}
        plugged = replace(graph, nodes=tuple(
            replace(n, legacy_payload=payload) if n.node_id == node.node_id else n for n in graph.nodes))
        (picture, reason), = service.evaluate(plugged, f01_hdr_ramp(16), (node.node_id,)).values()
        assert picture is None and "param_domain_unsupported" in reason
