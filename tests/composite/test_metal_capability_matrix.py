"""Capability matrix of the composite Metal chain (plan 8.1, logic evidence).

One row per kind of graph, one column per configuration: preview and export
mode, with the caller having resolved animated parameters or not, and the
service gate (the switch, a missing device).  Each cell is the verdict of
``evaluate_metal_support`` -- supported, or refused with an issue that names
the node -- or of ``CompositeGraphRenderService.render_metal``.  No GPU is
needed: this is the decision logic, the evidence a machine without Metal
can give.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.features.composite.render.composite_graph_render_service import (
    CompositeGraphRenderService,
)
from src.features.composite.core.graph_preflight import MODE_EXPORT, MODE_PREVIEW
from src.features.composite.render.metal.node_support import (
    METAL_COMPOSITE_SUPPORTED_TYPES,
    evaluate_metal_support,
)

SUPPORTED = None       # the verdict "supported": no blocking issue


def constant(nid: str, rgba: tuple = (1.0, 1.0, 1.0, 1.0), **params) -> dict:
    base = {"width": 8, "height": 8, "mode": "solid", "color": rgba,
            "color2": (1.0, 1.0, 1.0, 1.0), "checker_size": 4}
    return {"id": nid, "type": "constant", "parameters": {**base, **params}}


def node(nid: str, ntype: str, **params) -> dict:
    return {"id": nid, "type": ntype, "parameters": dict(params)}


def conn(src: str, dst: str, dst_port: str = "image", src_port: str = "result") -> dict:
    return {"src_node_id": src, "src_port_name": src_port,
            "dst_node_id": dst, "dst_port_name": dst_port}


def graph(nodes: list, connections: list) -> dict:
    return {"nodes": nodes, "connections": connections, "graph_revision": 7}


def chain(*middle: dict) -> dict:
    """constant -> the given nodes -> output."""
    nodes = [constant("c"), *middle, node("o", "output")]
    ids = ["c", *(entry["id"] for entry in middle), "o"]
    return graph(nodes, [conn(a, b) for a, b in zip(ids, ids[1:])])


ANIMATED = {"id": "t", "type": "transform", "parameters": {"scale_x": 1.0},
            "keyframe_tracks": {"scale_x": {"keyframes": [
                {"frame": 0, "value": 1.0}, {"frame": 10, "value": 2.0}]}}}


def rows() -> dict[str, dict]:
    return {
        "constant -> output": chain(),
        "transform (sub-pixel move and scale)": chain(
            node("t", "transform", translate_x=3.0, translate_y=-2.0,
                 scale_x=1.37, scale_y=0.83, filter="linear")),
        "merge of two layers (over)": graph(
            [constant("a", (0.5, 0.0, 0.0, 0.5)), constant("b", (0.0, 0.0, 1.0, 1.0)),
             node("m", "merge", blend_mode="over", opacity=0.9), node("o", "output")],
            [conn("a", "m", "A"), conn("b", "m", "B"), conn("m", "o")]),
        "crop, premultiply, unpremultiply, rectangle mask": graph(
            [constant("c"), node("k", "crop", left=0.1, top=0.1, right=0.0, bottom=0.0),
             node("p", "premult"), node("u", "unpremult"),
             node("mk", "mask", mask_type="rectangle"), node("m", "merge"),
             node("o", "output")],
            [conn("c", "k"), conn("k", "p"), conn("p", "u"), conn("u", "m", "A"),
             conn("mk", "m", "mask"), conn("m", "o")]),
        "keyer (not migrated)": chain(node("key", "keyer")),
        "blur (not migrated)": chain(node("b", "blur", radius=4.0)),
        # Sources of their own: nothing feeds them, they feed the output.
        "text (not migrated)": graph([node("txt", "text"), node("o", "output")],
                                     [conn("txt", "o")]),
        "image input (not migrated)": graph(
            [node("in", "image_input", path="clip.exr"), node("o", "output")],
            [conn("in", "o", src_port="image")]),
        "ellipse mask (parameter not migrated)": chain(
            node("mk", "mask", mask_type="ellipse")),
        "transform with motion blur (parameter not migrated)": chain(
            node("t", "transform", motion_blur=True)),
        "3D scene (cannot be mapped)": graph(
            [node("geo", "geo3d"), node("s3", "scene3d"), node("o", "output")],
            [conn("geo", "s3", "in", src_port="display"),
             conn("s3", "o", src_port="display")]),
        "temporal node (deflicker, needs earlier frames)": chain(
            node("df", "deflicker", window=9)),
        "animated parameter, unresolved by the caller": chain(ANIMATED),
        "structural error (cycle)": graph(
            [constant("c"), node("t1", "transform"), node("t2", "transform"),
             node("o", "output")],
            [conn("c", "t1"), conn("t1", "t2"), conn("t2", "t1"), conn("t2", "o")]),
        "unmigrated node outside the rendered cone": graph(
            [constant("c"), node("o", "output"), node("unused", "keyer")],
            [conn("c", "o")]),
    }


#: row -> (blocking issue code, node id); SUPPORTED when the chain takes it.
EXPECTED: dict[str, object] = {
    "constant -> output": SUPPORTED,
    "transform (sub-pixel move and scale)": SUPPORTED,
    "merge of two layers (over)": SUPPORTED,
    "crop, premultiply, unpremultiply, rectangle mask": SUPPORTED,
    "keyer (not migrated)": ("node.metal_unmigrated", "key"),
    "blur (not migrated)": ("node.metal_unmigrated", "b"),
    "text (not migrated)": ("node.metal_unmigrated", "txt"),
    "image input (not migrated)": ("node.metal_unmigrated", "in"),
    "ellipse mask (parameter not migrated)": ("node.metal_param_unsupported", "mk"),
    "transform with motion blur (parameter not migrated)": ("node.metal_param_unsupported", "t"),
    "3D scene (cannot be mapped)": ("node.metal_3d_unmapped", "s3"),
    "temporal node (deflicker, needs earlier frames)": ("node.metal_history_span", "df"),
    "animated parameter, unresolved by the caller": ("node.metal_animated_params", "t"),
    "structural error (cycle)": ("", ""),            # any structural issue; checked below
    "unmigrated node outside the rendered cone": SUPPORTED,
}

#: The only row whose verdict depends on the caller having resolved animation.
RESOLVED_TAKES_IT = "animated parameter, unresolved by the caller"


def _verdict(graph_data: dict, mode: str, resolved: bool) -> object:
    """``SUPPORTED``, or the ``(code, node id)`` of every blocking issue."""
    report = evaluate_metal_support(graph_data, mode=mode, params_resolved=resolved)
    if report.supported:
        return SUPPORTED
    return tuple(sorted((issue.code, issue.node_id) for issue in report.issues))


@pytest.mark.parametrize("mode", [MODE_PREVIEW, MODE_EXPORT], ids=["preview", "export"])
@pytest.mark.parametrize("resolved", [False, True], ids=["base-values", "animation-resolved"])
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_graph_gets_the_verdict_the_matrix_states(name: str, resolved: bool, mode: str) -> None:
    verdict = _verdict(rows()[name], mode, resolved)
    if name == RESOLVED_TAKES_IT and resolved:
        # Keyframes sampled by the existing evaluator: the chain takes it.
        assert verdict == SUPPORTED
        return
    expected = EXPECTED[name]
    if expected == SUPPORTED:
        assert verdict == SUPPORTED, (name, verdict)
    elif expected == ("", ""):
        assert verdict != SUPPORTED
        assert any("结构预检错误" in issue.reason for issue in evaluate_metal_support(
            rows()[name], mode=mode, params_resolved=resolved).issues)
    else:
        # The refusal names the node: never a silent skip of the node.  For a
        # chain of unmigrated nodes every one of them is listed.
        assert expected in verdict, (name, verdict)
        assert expected[1] in evaluate_metal_support(
            rows()[name], mode=mode, params_resolved=resolved).fallback_summary


def test_every_refusal_code_of_the_chain_appears_in_the_matrix() -> None:
    codes = {value[0] for value in EXPECTED.values() if value != SUPPORTED and value != ("", "")}
    assert codes == {
        "node.metal_unmigrated", "node.metal_param_unsupported",
        "node.metal_3d_unmapped", "node.metal_history_span", "node.metal_animated_params",
    }


def test_the_migrated_types_are_exactly_the_ones_the_matrix_takes() -> None:
    assert METAL_COMPOSITE_SUPPORTED_TYPES == {
        "constant", "transform", "merge", "output", "crop",
        "premult", "unpremult", "mask", "noop", "dot",
    }


class TestServiceGate:
    """``render_metal`` in front of the chain: the switch, then the device."""

    @pytest.fixture()
    def service(self) -> CompositeGraphRenderService:
        return CompositeGraphRenderService()

    def test_off_by_default(self, service: CompositeGraphRenderService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOPOS_COMPOSITE_METAL", raising=False)
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        assert service.render_metal(rows()["constant -> output"]) is None
        assert service._metal_renderer is None            # nothing was built

    def test_the_switch_off_keeps_the_graph_on_cpu_or_gl(
        self, service: CompositeGraphRenderService, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "composite_metal")
        assert service.render_metal(rows()["constant -> output"]) is None
        assert service._metal_renderer is None

    def test_without_a_device_nothing_is_built(
        self, service: CompositeGraphRenderService, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.shared.gpu.metal import MetalComputeContext

        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        with patch.object(MetalComputeContext, "is_supported", return_value=False):
            assert service.render_metal(rows()["constant -> output"]) is None
        assert service._metal_renderer is None

    def test_an_empty_graph_is_refused_before_any_device_work(
        self, service: CompositeGraphRenderService, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.shared.gpu.metal import MetalComputeContext

        monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        with patch.object(MetalComputeContext, "is_supported", return_value=True), \
                patch("src.features.composite.render.metal.renderer.MetalCompositeRenderer") as renderer:
            # Not a graph at all: refused before anything is built.
            assert service.render_metal({}) is None
            assert service.render_metal("not a graph") is None
            renderer.assert_not_called()
            # A graph without nodes reaches the chain, which answers with an
            # explicitly unavailable handle rather than a picture.
            service.render_metal({"nodes": []})
            renderer.assert_called_once()


def test_the_matrix_needs_no_gpu() -> None:
    """The whole table is decision logic: it must not touch Metal."""
    from src.shared.gpu.metal import MetalComputeContext

    with patch.object(MetalComputeContext, "is_supported", side_effect=AssertionError("touched Metal")):
        for name in EXPECTED:
            for mode in (MODE_PREVIEW, MODE_EXPORT):
                _verdict(rows()[name], mode, False)
