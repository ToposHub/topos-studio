"""Named composite output target helpers.

Output nodes are terminal nodes and therefore do not expose a normal image
output port to downstream graph nodes. A named target is the stable identity
used by the viewer/export request to choose one of several terminal outputs.
"""

from __future__ import annotations

from typing import Any, Iterable


DEFAULT_OUTPUT_NAME = "display"


def _read_value(value: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a live node/snapshot or serialized data."""

    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _node_type(value: Any) -> str:
    """Read the live ``node_type`` or serialized ``type`` field."""

    return str(_read_value(value, "node_type", None)
               or _read_value(value, "type", ""))


def normalize_output_name(value: object) -> str:
    """Return a stable, human-readable output name.

    Names are intentionally not restricted to an enum: productions commonly
    use names such as ``beauty``, ``matte.green`` or ``shot010_fg``. Empty or
    whitespace-only legacy values map to the backwards-compatible display
    target.
    """

    name = str(value or "").strip()
    return name or DEFAULT_OUTPUT_NAME


def output_name_for_node(node_or_snapshot: Any) -> str:
    """Read a terminal node's name from either a Node or NodeSnapshot."""

    parameters = _read_value(node_or_snapshot, "parameters", {}) or {}
    if not isinstance(parameters, dict):
        parameters = {}
    return normalize_output_name(parameters.get("output_name"))


def output_target_records(nodes: Iterable[Any]) -> tuple[dict[str, str], ...]:
    """Return deterministic named-output records for nodes in graph order.

    Duplicate names are preserved because two terminal nodes may be useful as
    alternate versions of the same target. Callers can disambiguate with the
    returned node id; the first record remains the default name target.
    """

    records: list[dict[str, str]] = []
    for node in nodes:
        if _node_type(node) != "output":
            continue
        node_id = str(_read_value(node, "id", None)
                      or _read_value(node, "node_id", ""))
        parameters = _read_value(node, "parameters", {}) or {}
        if not isinstance(parameters, dict):
            parameters = {}
        records.append({
            "name": normalize_output_name(parameters.get("output_name")),
            "node_id": node_id,
            "node_type": _node_type(node),
        })
    return tuple(records)


def output_dependency_records(
    nodes: Iterable[Any],
    connections: Iterable[Any],
) -> tuple[dict[str, Any], ...]:
    """Return the transitive input dependencies for every named Output node.

    The result is JSON-safe and accepts both live graph objects and serialized
    dictionaries. ``dependency_node_ids`` excludes the terminal output itself
    and follows graph node order, making it suitable for diagnostics, cache
    planning, and export manifests.
    """

    node_list = list(nodes)
    node_order = {
        str(_read_value(node, "id", None)
            or _read_value(node, "node_id", "")): index
        for index, node in enumerate(node_list)
    }
    upstream: dict[str, list[str]] = {
        node_id: [] for node_id in node_order
    }
    for connection in connections:
        source_id = str(_read_value(connection, "src_node_id", ""))
        destination_id = str(_read_value(connection, "dst_node_id", ""))
        if source_id in node_order and destination_id in node_order:
            upstream.setdefault(destination_id, []).append(source_id)

    records: list[dict[str, Any]] = []
    for target in output_target_records(node_list):
        output_id = target["node_id"]
        direct_inputs = list(dict.fromkeys(upstream.get(output_id, [])))
        visited: set[str] = set()
        stack = list(reversed(direct_inputs))
        while stack:
            node_id = stack.pop()
            if node_id in visited or node_id == output_id:
                continue
            visited.add(node_id)
            stack.extend(reversed(upstream.get(node_id, [])))
        dependency_ids = sorted(
            visited, key=lambda node_id: node_order.get(node_id, len(node_order)))
        records.append({
            **target,
            "direct_input_node_ids": direct_inputs,
            "dependency_node_ids": dependency_ids,
            "dependency_count": len(dependency_ids),
        })
    return tuple(records)


def selected_output_dependency_ids(
    nodes: Iterable[Any],
    connections: Iterable[Any],
    output_name: object = DEFAULT_OUTPUT_NAME,
    output_node_id: object = None,
) -> frozenset[str] | None:
    """Resolve one named Output target and return its evaluation dependency set.

    ``None`` means the requested target is not present; callers should retain
    their legacy fallback behavior in that case. A resolved set includes the
    terminal node itself, so an evaluator can skip unrelated branches without
    changing output selection semantics.
    """

    normalized_name = normalize_output_name(output_name)
    target_id = str(output_node_id or "")
    records = output_dependency_records(nodes, connections)
    for record in records:
        if record["name"] != normalized_name:
            continue
        if target_id and record["node_id"] != target_id:
            continue
        return frozenset({
            str(record["node_id"]),
            *(str(node_id) for node_id in record["dependency_node_ids"]),
        })
    return None


__all__ = [
    "DEFAULT_OUTPUT_NAME",
    "normalize_output_name",
    "output_name_for_node",
    "output_target_records",
    "output_dependency_records",
    "selected_output_dependency_ids",
]
