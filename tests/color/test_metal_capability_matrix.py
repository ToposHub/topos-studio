"""Capability matrix of the colour graph (plan 8.1, logic evidence).

The node types of a colour graph are in the capability catalog of all three
backends alike, so a plan compiles for Metal, GL and CPU whatever the graph
holds.  What differs is the *domain* of a node's parameters: the Metal
texture executor takes the migrated domains and refuses the rest, by name.
One row per domain, one column per question:

* ``domain`` -- what the executor's own check says about the parameter
  payload a node hands to the executor (pure data, no GPU);
* ``plan`` -- whether the plan compiles for metal_texture, gl and cpu;
* ``frame`` -- what the shared entry does with the frame on this machine:
  ``metal_texture``, the compatible CPU graph (``cpu_compat``), or an
  explicit failure that publishes nothing (``none``).  Needs a device and
  a graph the project can actually hold; rows that are only an internal
  invariant have no graph and are not asked.

A refusal is never a silent approximation: either the compatible graph
produces the frame, or nothing is published.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from typing import Any, Optional

import numpy as np
import pytest

from src.features.color.core.color_graph_entry import (
    SharedColorGraphEntry,
    plan_color_graph_frame,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_texture_metal import texture_layer_domain_errors
from src.features.color.core.layer import build_curves_lut_from_data
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.project.color_graph import NodeType
from tests.color.fixtures.color_graph import _neutral_layer, f01_hdr_ramp
from tests.color.test_color_graph_migration import _stack_with_layers
from tests.color.test_color_graph_texture_metal_masks import _CIRCLE, _corrector, _serial_graph

TAKEN = ""            # the domain check has nothing to say: the executor takes it


def _graded(**params: float) -> object:
    layer = _neutral_layer("Grade")
    layer.grading_params.update(params)
    return layer


def _curves() -> object:
    layer = _neutral_layer("Curve")
    layer.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    layer.curves_lut = build_curves_lut_from_data(layer.curves_data)
    return layer


def _fx_blur_layer() -> object:
    layer = _neutral_layer("Fx")
    layer.effects_params["blur_enabled"] = True
    return layer


def _migrated(layers: list) -> Any:
    return migrate_legacy_stack(_stack_with_layers(layers))


def _corrector_id(graph: Any) -> str:
    return next(n.node_id for n in graph.nodes if n.node_type == NodeType.CORRECTOR.value)


def _payload_of(graph: Any) -> dict:
    """What the executor is handed for the first corrector of ``graph``."""
    payload = graph_node_layer_provider(graph, frame=0)(_corrector_id(graph))
    assert payload is not None
    return dict(payload)


def _with_plugin(graph: Any) -> Any:
    node_id = _corrector_id(graph)
    payload = {**dict(next(n for n in graph.nodes if n.node_id == node_id).legacy_payload or {}),
               "plugins": [{"name": "nope", "plugin_id": "x", "instance_id": "i"}]}
    return _replace(graph, nodes=tuple(
        _replace(n, legacy_payload=payload) if n.node_id == node_id else n
        for n in graph.nodes))


class Row:
    """One domain: the graph that carries it (or none) and the refusal."""

    def __init__(self, graph: Any, refusal: str = TAKEN, *,
                 payload: Optional[dict] = None) -> None:
        self.graph = graph
        self.refusal = refusal
        self._payload = payload

    def payload(self) -> dict:
        if self._payload is not None:
            return self._payload
        return _payload_of(self.graph)


def rows() -> dict[str, Row]:
    graded = _payload_of(_migrated([_graded(exposure=8.0)]))
    return {
        "neutral corrector": Row(_migrated([_neutral_layer("N")])),
        "primary grade": Row(_migrated([_graded(exposure=25.0, contrast=30.0, saturation=-18.0,
                                               vibrance=25.0, hue_shift=40.0)])),
        "RGB curves": Row(_migrated([_curves()])),
        "two graders in series": Row(_migrated([_graded(exposure=8.0), _curves()])),
        "qualifier with a window": Row(_serial_graph([_corrector(
            "q", qualifier={"enabled": True, "hue_center": 0.3}, shapes=[_CIRCLE])])),
        "plugin": Row(_with_plugin(_migrated([_graded(exposure=8.0)])), "插件"),
        "neighbourhood grade (sharpness, mid-detail)": Row(
            _migrated([_graded(sharpness=5.0)]), "邻域"),
        "qualifier post-process (clean / blur / denoise)": Row(
            _serial_graph([_corrector("q", qualifier={"enabled": True, "blur": 12.0})]),
            "后处理"),
        "fx / blur group switched on": Row(_migrated([_fx_blur_layer()]), "fx/blur"),
        # Invariants of the payload rather than anything a project can hold:
        # checked at the domain level, with the payload spelled out.
        "warper in a second colour space": Row(
            None, "多空间",
            payload={**graded, "warper_params": np.zeros((4, 4, 2), np.float32),
                     "warper_space": 2}),
        "curve data without its built LUT": Row(
            None, "未预构建", payload={**graded, "curves_data": {"M": [(0, 255)]},
                                      "curves_lut": None}),
        "grading parameter nobody knows": Row(
            None, "未支持", payload={"grading_params": {"no_such_knob": 1.0}}),
    }


@pytest.mark.parametrize("row", sorted(rows()))
def test_the_domain_check_gives_the_verdict_the_matrix_states(row: str) -> None:
    entry = rows()[row]
    errors = texture_layer_domain_errors(entry.payload())
    if entry.refusal == TAKEN:
        assert errors == [], (row, errors)
    else:
        # The refusal names the domain; it is not a bare "unsupported".
        assert any(entry.refusal in error for error in errors), (row, errors)


@pytest.mark.parametrize("backend", ["metal_texture", "gl", "cpu"])
@pytest.mark.parametrize("row", sorted(rows()))
def test_the_plan_compiles_for_every_backend_whatever_the_domain(row: str, backend: str) -> None:
    """Node types are catalogued for all three backends; the domain decides
    at execution time, so a refused domain is not a compile error."""
    graph = rows()[row].graph
    if graph is None:
        pytest.skip("an invariant of the payload; it has no graph")
    snapshot = freeze_graph(graph, session_id="matrix", source_frame=0)
    outcome = plan_color_graph_frame(snapshot, size=(64, 48), backend=backend)
    assert outcome.plan is not None, (row, backend, outcome.compile_error)
    assert outcome.capability is not None and outcome.capability.executable, (row, backend)


@pytest.mark.skipif(not MetalComputeContext.is_supported(), reason="no Metal device")
@pytest.mark.parametrize("row", sorted(rows()))
def test_what_the_shared_entry_does_with_the_frame(row: str) -> None:
    entry_row = rows()[row]
    if entry_row.graph is None:
        pytest.skip("an invariant of the payload; it has no graph")
    entry = SharedColorGraphEntry(MetalComputeContext())
    try:
        result = entry.render_frame(entry_row.graph, 0, f01_hdr_ramp(32), media_id="matrix")
    finally:
        if entry._texture_executor is not None:
            entry._texture_executor.release_revision("matrix")
    if entry_row.refusal == TAKEN:
        assert result.published and result.backend == "metal_texture", (row, result)
        assert result.fallback_reason == ""
        return
    # The executor refuses the domain.  Either the compatible graph renders
    # the frame -- and says it fell back -- or nothing is published.
    assert result.backend != "metal_texture", (row, result)
    if result.backend == "cpu_compat":
        assert result.published and result.fallback_reason, (row, result)
        assert any("param_domain_unsupported" in item for item in result.diagnostics), (row, result)
    else:
        assert result.backend == "none" and not result.published and result.output is None, (row, result)
        assert result.diagnostics, row


def test_the_rows_cover_every_refusal_the_domain_check_can_give() -> None:
    refusals = {row.refusal for row in rows().values()} - {TAKEN}
    assert refusals == {"插件", "邻域", "后处理", "多空间", "fx/blur", "未预构建", "未支持"}
