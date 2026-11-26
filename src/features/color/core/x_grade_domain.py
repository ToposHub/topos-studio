"""Guard Selective Grade's Rec.709 working domain without modifying other grades."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import numpy as np

from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, ColorSpaceDescriptor, descriptor_for_space_preset,
)
from src.shared.project.color_graph import ColorGraphData


X_GRADE_WORKING_KEY = "x_grade_working_supported"
X_GRADE_WORKING_MESSAGE = "Selective Grade requires linear Rec.709 or Rec.709 Gamma 2.4."
# The rectangular scope presents the stored chromaticities along these two axes.
X_GRADE_SCOPE_AXES = np.array(((-0.88, -0.98), (1.06, -0.62)))
_GAMMA24 = descriptor_for_space_preset("rec709-g24")


def x_grade_working_supported(space: ColorSpaceDescriptor | None) -> bool:
    """Allow the explicit Rec.709 domains and the historical untagged path."""
    return space is None or space in (LINEAR_SCENE, _GAMMA24)


def has_active_x_grade(payload: Mapping[str, Any] | None) -> bool:
    if not payload:
        return False
    from src.features.color.core.warper_color import WARPER_X_GRADE_SPACE

    if payload.get("warper_space") == WARPER_X_GRADE_SPACE and payload.get("warper_params") is not None:
        return True
    state = payload.get("warper_state")
    grade = state.get("x_grade") if isinstance(state, Mapping) else None
    strokes = grade.get("strokes") if isinstance(grade, Mapping) else None
    return isinstance(strokes, (list, tuple)) and bool(strokes)


def active_x_grade_node(graph: ColorGraphData) -> str | None:
    from src.features.color.core.color_graph_param_source import _iter_corrector_nodes

    for node in _iter_corrector_nodes(graph):
        # Graph warper edits store all three Warper keys together in the
        # corrector's legacy payload, including for shared correctors.
        if node.enabled and has_active_x_grade(node.legacy_payload):
            return node.node_id
    return None


def x_grade_preview_graph(graph: ColorGraphData, *, supported: bool) -> ColorGraphData:
    """Tag an execution-only graph. The authoritative project stays editable."""
    if supported or active_x_grade_node(graph) is None:
        return graph
    return replace(graph, working_color_context={**graph.working_color_context, X_GRADE_WORKING_KEY: False})


def bypass_x_grade(payload: dict[str, Any]) -> None:
    """Substitute only the underlying Warper projection in an execution DTO."""
    from src.features.color.core.warper_color import WARPER_X_GRADE_SPACE

    if payload.get("warper_space") != WARPER_X_GRADE_SPACE:
        return
    from src.features.color.core.x_grade_render import unpack_x_grade_base

    payload["warper_params"], payload["warper_space"] = unpack_x_grade_base(payload["warper_params"])
