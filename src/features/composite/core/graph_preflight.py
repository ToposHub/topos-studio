"""Graph preflight — pre-render diagnostics for the composite graph.

Plan P0-03: ``NodeGraph.validate()`` only checked cycles and unconnected
Output nodes.  Professional compositors tell the user *before* rendering
what will fail.  This module is a pure analysis service (no Qt, no GL,
no filesystem mutation) that produces a stable, categorized issue list
with node targets and fix suggestions.

Two entry points:

* :func:`run_preflight_data` scans the serialized ``NodeGraph.to_dict()``
  form — this is what the export path guards on (the render already works
  on a stable dict copy of the graph).
* :func:`run_preflight` accepts a live :class:`NodeGraph` and serializes
  it first (layers included).

Severity policy (P0-06 placeholder governance):

* ``error``   — the render cannot produce a trustworthy result (cycle,
                Output without input, missing media path, placeholder
                node in the render tree in export mode).  ``ok`` is False
                and the export path must not start.
* ``warning`` — result may be wrong or degraded (placeholder in preview
                mode, file missing on disk, inverted layer range, layer
                referencing missing nodes).
* ``info``    — hygiene observations (orphan nodes, branches that do not
                reach an Output, multiple Outputs).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .node_capabilities import get_node_capability, is_placeholder_node_type
from .asset_dependency import (
    ROLE_LUT,
    ROLE_MEDIA,
    ROLE_MODEL,
    STATUS_MISSING,
    STATUS_MISSING_PATH,
    STATUS_RELINK_CANDIDATE,
    AssetDependencyReport,
    collect_asset_dependencies,
)
from ..utils.expression_parser import ExpressionParser
from ..render3d.format_capabilities import probe_import
from ..render3d.aov import AOV_EXPORTABLE_PASSES
from .output_targets import output_dependency_records

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_OUTPUT_NODE_TYPES = frozenset({"output", "file_output"})

# Modes: "preview" (interactive viewer) vs "export" (final render).
# Placeholder nodes in the render tree degrade to a warning in preview but
# block the final render (P0-06: 占位节点不能静默混入最终结果).
MODE_PREVIEW = "preview"
MODE_EXPORT = "export"


@dataclass(frozen=True)
class PreflightIssue:
    """One diagnosable problem with a stable code for tests/UI/log parity."""
    code: str
    severity: str
    message: str
    node_id: str = ""
    suggestion: str = ""

    def format(self) -> str:
        target = f" [{self.node_id}]" if self.node_id else ""
        suffix = f" — {self.suggestion}" if self.suggestion else ""
        return f"{self.message}{target}{suffix}"


@dataclass
class PreflightReport:
    issues: List[PreflightIssue] = field(default_factory=list)
    # P1-14: retain the structured dependency scan so offline/relink UI and
    # future collect commands can consume the same result as preflight.
    asset_dependencies: AssetDependencyReport = field(
        default_factory=AssetDependencyReport)
    # P2-05: structured terminal-output dependency cones.  This is shared by
    # the UI diagnostics, targeted evaluation, and future batch export code.
    output_dependencies: Tuple[Dict[str, object], ...] = ()

    @property
    def ok(self) -> bool:
        """True when no error-severity issue blocks rendering."""
        return not any(i.severity == SEVERITY_ERROR for i in self.issues)

    def by_severity(self, severity: str) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == severity]

    @property
    def errors(self) -> List[PreflightIssue]:
        return self.by_severity(SEVERITY_ERROR)

    @property
    def warnings(self) -> List[PreflightIssue]:
        return self.by_severity(SEVERITY_WARNING)

    def format_summary(self, max_items: int = 12) -> str:
        """Human-readable summary for dialogs and logs."""
        if not self.issues:
            return "OK"
        lines: List[str] = []
        for severity, label in ((SEVERITY_ERROR, "ERROR"),
                                (SEVERITY_WARNING, "WARN"),
                                (SEVERITY_INFO, "INFO")):
            items = self.by_severity(severity)
            if not items:
                continue
            lines.append(f"{label} ({len(items)}):")
            for issue in items[:max_items]:
                lines.append(f"  [{issue.code}] {issue.format()}")
            if len(items) > max_items:
                lines.append(f"  … {len(items) - max_items} more")
        return "\n".join(lines)


# ── Scanner ────────────────────────────────────────────────────────────────

def run_preflight_data(graph_data: Optional[dict],
                       mode: str = MODE_PREVIEW,
                       check_fs: bool = True,
                       project_dir: Optional[str] = None,
                       asset_search_roots: Iterable[str] = ()) -> PreflightReport:
    """Scan serialized graph data (``NodeGraph.to_dict()`` form).

    ``project_dir`` and ``asset_search_roots`` are optional relink hints. They are
    only consulted when ``check_fs`` is enabled, and resolving a candidate never
    mutates the graph or filesystem.
    """
    report = PreflightReport()
    if not graph_data:
        report.issues.append(PreflightIssue(
            code="graph.empty", severity=SEVERITY_ERROR,
            message="合成图为空，无法渲染。"))
        return report

    nodes = graph_data.get("nodes") or []
    connections = graph_data.get("connections") or []
    report.asset_dependencies = collect_asset_dependencies(
        graph_data, check_fs=check_fs, project_dir=project_dir,
        asset_search_roots=asset_search_roots)

    node_types: Dict[str, str] = {}
    for nd in nodes:
        nid = str(nd.get("id", ""))
        node_types[nid] = str(nd.get("type", ""))
    node_ids: Set[str] = set(node_types)

    # Group/SubGraph boundaries are virtual render edges. Validate the
    # contract before topology analysis, then use the same flattened edges
    # as GraphSnapshot so diagnostics do not report every valid member as an
    # unreachable branch.
    _check_groups(nodes, node_ids, report)
    _check_precomps(nodes, graph_data, node_ids, report)
    _check_expressions(nodes, node_ids, report)
    _check_layer_chain_conflicts(graph_data, node_ids, connections, report)
    effective_connections = _flatten_group_connections(nodes, connections)
    _check_connection_ports(nodes, node_types, effective_connections, report)

    # ── Topology: cycles (Kahn, mirrors NodeGraph.topological_sort) ──
    in_degree = {nid: 0 for nid in node_ids}
    downstream: Dict[str, List[str]] = {nid: [] for nid in node_ids}
    for conn in effective_connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        if src in node_ids and dst in node_ids:
            downstream[src].append(dst)
            in_degree[dst] += 1
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    visited: Set[str] = set()
    while queue:
        nid = queue.pop()
        visited.add(nid)
        for nb in downstream[nid]:
            in_degree[nb] -= 1
            if in_degree[nb] == 0:
                queue.append(nb)
    if len(visited) != len(node_ids):
        cyclic = sorted(nid for nid in node_ids if nid not in visited)
        for nid in cyclic:
            report.issues.append(PreflightIssue(
                code="graph.cycle", severity=SEVERITY_ERROR,
                message="图中存在环路，无法确定求值顺序。", node_id=nid,
                suggestion="移除形成环的连接。"))

    # ── Dangling connections ──
    for conn in connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        for endpoint in (src, dst):
            if endpoint and endpoint not in node_ids:
                report.issues.append(PreflightIssue(
                    code="connection.dangling", severity=SEVERITY_WARNING,
                    message=f"连接引用了不存在的节点 {endpoint!r}。",
                    node_id=endpoint,
                    suggestion="重新连接或删除该连接。"))

    # ── Output contract ──
    output_ids = [nid for nid, ntype in node_types.items()
                  if ntype in _OUTPUT_NODE_TYPES]
    if nodes and not output_ids:
        report.issues.append(PreflightIssue(
            code="output.missing", severity=SEVERITY_WARNING,
            message="图中没有输出节点，渲染没有输出面。",
            suggestion="添加一个 Output 节点并连接到最终结果。"))
    fed_outputs: Set[str] = set()
    for conn in effective_connections:
        dst = str(conn.get("dst_node_id", ""))
        if dst in output_ids:
            fed_outputs.add(dst)
    for nid in output_ids:
        if nid not in fed_outputs:
            report.issues.append(PreflightIssue(
                code="output.no_input", severity=SEVERITY_ERROR,
                message="输出节点没有输入连接，无法渲染。",
                node_id=nid, suggestion="将合成结果连接到该节点。"))
    if len(output_ids) > 1:
        report.issues.append(PreflightIssue(
            code="output.multiple", severity=SEVERITY_INFO,
            message=f"图中存在 {len(output_ids)} 个 Output 节点，"
                    f"渲染使用第一个找到的。"))

    report.output_dependencies = output_dependency_records(
        nodes, effective_connections)
    names: Dict[str, List[str]] = {}
    for target in report.output_dependencies:
        names.setdefault(str(target["name"]), []).append(
            str(target["node_id"]))
    for name, target_ids in names.items():
        if len(target_ids) < 2:
            continue
        report.issues.append(PreflightIssue(
            code="output.duplicate_name", severity=SEVERITY_WARNING,
            message=(f"Output 名称 {name!r} 被多个终端节点使用。"),
            node_id=target_ids[0],
            suggestion=("通过 output_node_id 区分目标，或为每个 Output 设置唯一名称。")))

    # ── Reachability: which nodes feed an Output ──
    upstream: Dict[str, List[str]] = {nid: [] for nid in node_ids}
    for conn in effective_connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        if src in node_ids and dst in node_ids:
            upstream[dst].append(src)
    contributing: Set[str] = set()
    stack = list(output_ids)
    while stack:
        nid = stack.pop()
        if nid in contributing:
            continue
        contributing.add(nid)
        stack.extend(upstream.get(nid, []))

    _check_render_pass_contract(
        nodes, node_types, contributing, graph_data, mode, report)

    connected_ids: Set[str] = set()
    for conn in effective_connections:
        connected_ids.add(str(conn.get("src_node_id", "")))
        connected_ids.add(str(conn.get("dst_node_id", "")))

    for nid, ntype in sorted(node_types.items()):
        if ntype in {"output", "group"}:
            continue
        if nid not in contributing:
            if nid not in connected_ids:
                report.issues.append(PreflightIssue(
                    code="node.orphan", severity=SEVERITY_INFO,
                    message="节点未连接到任何东西，不参与渲染。",
                    node_id=nid))
            else:
                report.issues.append(PreflightIssue(
                    code="node.unreachable", severity=SEVERITY_INFO,
                    message="节点所在的分支不会到达 Output 节点。",
                    node_id=nid))

    # ── Unknown / placeholder nodes (P0-06) ──
    for nid, ntype in sorted(node_types.items()):
        cap = get_node_capability(ntype)
        if cap is None:
            report.issues.append(PreflightIssue(
                code="node.unknown_type", severity=SEVERITY_WARNING,
                message=f"未知节点类型 {ntype!r}，无法求值。",
                node_id=nid,
                suggestion="安装对应的节点插件或删除该节点。"))
            continue
        if not is_placeholder_node_type(ntype):
            continue
        in_tree = nid in contributing
        severity = (SEVERITY_ERROR if (in_tree and mode == MODE_EXPORT)
                    else (SEVERITY_WARNING if in_tree else SEVERITY_INFO))
        report.issues.append(PreflightIssue(
            code="node.placeholder", severity=severity,
            message=f"{ntype!r} 是占位节点，尚未实现真实效果"
                    f"{'，最终渲染将被阻断' if severity == SEVERITY_ERROR else ''}。",
            node_id=nid,
            suggestion="移除或绕过该节点后再渲染。"))

    # ── Asset dependencies (source, LUT, HDRI, IES, material overrides) ──
    # The collector above is the single path-resolution source for both the
    # structured report and user-facing issues.  This keeps relink candidates
    # deterministic and avoids source-node-only blind spots.
    for reference in report.asset_dependencies.references:
        is_source = reference.role in (ROLE_MEDIA, ROLE_MODEL)
        if reference.status == STATUS_MISSING_PATH:
            if is_source:
                report.issues.append(PreflightIssue(
                    code="source.missing_path", severity=SEVERITY_ERROR,
                    message=(f"{reference.node_type!r} 节点没有设置"
                             f"{reference.purpose}路径。"),
                    node_id=reference.node_id,
                    suggestion="重新指定媒体文件。"))
            else:
                # Optional auxiliary features (IES/IBL) currently degrade to
                # a default light when their path is empty; report that loss
                # without blocking an otherwise renderable graph.
                report.issues.append(PreflightIssue(
                    code="asset.missing_path", severity=SEVERITY_WARNING,
                    message=(f"{reference.node_type!r} 节点启用了"
                             f"{reference.purpose}但未设置路径。"),
                    node_id=reference.node_id,
                    suggestion="指定资源路径，或关闭该辅助功能。"))
            continue

        if reference.status == STATUS_MISSING:
            report.issues.append(PreflightIssue(
                code=("source.file_not_found" if is_source
                      else "asset.file_not_found"),
                severity=SEVERITY_WARNING,
                message=f"{reference.purpose}文件不存在：{reference.stored_path}",
                node_id=reference.node_id,
                suggestion="检查文件是否被移动，或使用依赖报告重链接素材。"))
            continue

        if reference.status == STATUS_RELINK_CANDIDATE:
            report.issues.append(PreflightIssue(
                code=("source.relink_candidate" if is_source
                      else "asset.relink_candidate"),
                severity=SEVERITY_INFO,
                message=(f"发现{reference.purpose}重链接候选路径："
                         f"{reference.resolved_path}"),
                node_id=reference.node_id,
                suggestion="确认后由重链接命令更新节点资源路径。"))

        if (reference.role == ROLE_LUT and reference.resolved_path
                and os.path.splitext(reference.resolved_path)[1].lower()
                not in (".cube", ".3dl")):
            report.issues.append(PreflightIssue(
                code="asset.unsupported_format", severity=SEVERITY_WARNING,
                message=(f"LUT 文件格式不受支持：{reference.resolved_path}"),
                node_id=reference.node_id,
                suggestion="使用 .cube 或 .3dl 格式，避免节点静默直通。"))

        if reference.role != ROLE_MODEL or not reference.resolved_path:
            continue
        probe = probe_import(reference.resolved_path, require_exists=True)
        model_issue = {
            "format.unknown": (
                "source.unsupported_format",
                "模型文件格式未注册：{path}",
                "导出为 glTF/GLB/FBX 等已支持格式。"),
            "format.planned": (
                "source.format_not_implemented",
                "模型格式暂未接入导入后端：{path}",
                "改用当前支持的格式，或安装/接入对应 loader。"),
            "backend.unavailable": (
                "source.loader_unavailable",
                "模型导入后端不可用：{path}",
                "安装对应依赖后重新预检。"),
        }.get(probe.reason_code)
        if model_issue is not None:
            code, message, suggestion = model_issue
            report.issues.append(PreflightIssue(
                code=code, severity=SEVERITY_WARNING,
                message=message.format(path=reference.resolved_path),
                node_id=reference.node_id, suggestion=suggestion))

    # File Output is a destination rather than an input dependency, so it
    # remains in preflight's output contract instead of the asset report.
    for nd in nodes:
        ntype = str(nd.get("type", ""))
        if ntype != "file_output":
            continue
        params = nd.get("parameters") or {}
        nid = str(nd.get("id", ""))
        path = str(params.get("file_path", "") or "")
        if path:
            continue
        in_tree = nid in contributing
        severity = (SEVERITY_ERROR if (in_tree and mode == MODE_EXPORT)
                    else (SEVERITY_WARNING if in_tree else SEVERITY_INFO))
        report.issues.append(PreflightIssue(
            code="output.missing_path", severity=severity,
            message="File Output 节点没有设置输出路径。",
            node_id=nid,
            suggestion="指定文件路径，或移除该输出节点。"))

    # ── Layer references ──
    layers = graph_data.get("layers") or {}
    _check_layers(layers, node_ids, report)
    _check_track_mattes(layers, graph_data.get("layer_stacks") or {}, report)
    return report


def _check_render_pass_contract(
    nodes: List[dict],
    node_types: Dict[str, str],
    contributing: Set[str],
    graph_data: dict,
    mode: str,
    report: PreflightReport,
) -> None:
    """Validate requested File Output passes before a render starts.

    The serialized graph can contain values written by an older version or an
    external tool, so the ParamDef option labels alone are not sufficient.
    This check mirrors the actual export selector and also verifies that a
    non-beauty pass has a contributing Scene3D source with that pass enabled.
    """
    exportable = {pass_type.value for pass_type in AOV_EXPORTABLE_PASSES}
    scene_ids = {
        node_id for node_id, node_type in node_types.items()
        if node_type == "scene3d" and node_id in contributing
    }
    viewer_state = graph_data.get("viewer_state") or {}
    if not isinstance(viewer_state, dict):
        viewer_state = {}
    scene_settings = viewer_state.get("default_3d_scene") or {}
    if not isinstance(scene_settings, dict):
        scene_settings = {}
    # These match merged_viewer3d_scene_settings() defaults.  A missing graph
    # key therefore still has a deterministic runtime meaning.
    enabled_by_default = {
        "depth": True,
        "object_id": True,
        "normal": False,
        "position": False,
        "uv": False,
        "material_id": False,
        "motion_vector": False,
    }

    # Scene3D auxiliary outputs are connectable image ports.  A serialized
    # graph may retain a connection after its optional pass was turned off,
    # so validate the port contract independently of FileOutput's selector.
    for connection in graph_data.get("connections") or ():
        if not isinstance(connection, dict):
            continue
        source_id = str(connection.get("src_node_id", ""))
        source_port = str(connection.get("src_port_name", ""))
        if (node_types.get(source_id) != "scene3d"
                or source_id not in scene_ids
                or source_port in ("", "display", "beauty")
                or source_port not in exportable):
            continue
        enabled = bool(scene_settings.get(
            f"aov_{source_port}", enabled_by_default.get(source_port, False)))
        if not enabled:
            _append_render_pass_issue(
                report, mode, source_id, "render_pass.port_disabled",
                f"Scene3D 输出端口 {source_port!r} 未启用对应 AOV。",
                f"打开 aov_{source_port}，或断开该 AOV 输出端口。")

    for node in nodes:
        if not isinstance(node, dict) or str(node.get("type", "")) != "file_output":
            continue
        node_id = str(node.get("id", ""))
        requested = str((node.get("parameters") or {}).get(
            "aov_pass", "beauty") or "beauty")
        if requested not in exportable:
            _append_render_pass_issue(
                report, mode, node_id, "render_pass.unsupported",
                f"File Output 请求了尚未支持的 Render Pass：{requested!r}。",
                "改用已实现的 pass，或等待该 AOV 后端接入。")
            continue
        if requested == "beauty" or node_id not in contributing:
            continue
        if not scene_ids:
            _append_render_pass_issue(
                report, mode, node_id, "render_pass.no_source",
                f"Render Pass {requested!r} 没有可贡献该 AOV 的 Scene3D 来源。",
                "连接 Scene3D 结果，或将输出 pass 改为 beauty。")
            continue
        setting_key = f"aov_{requested}"
        enabled = bool(scene_settings.get(
            setting_key, enabled_by_default.get(requested, False)))
        if not enabled:
            _append_render_pass_issue(
                report, mode, node_id, "render_pass.disabled",
                f"请求的 Render Pass {requested!r} 在 Scene3D 设置中未启用。",
                f"打开 {setting_key}，或将输出 pass 改为 beauty。")


def _append_render_pass_issue(
    report: PreflightReport,
    mode: str,
    node_id: str,
    code: str,
    message: str,
    suggestion: str,
) -> None:
    severity = SEVERITY_ERROR if mode == MODE_EXPORT else SEVERITY_WARNING
    report.issues.append(PreflightIssue(
        code=code, severity=severity, message=message,
        node_id=node_id, suggestion=suggestion))


def _check_connection_ports(nodes: List[dict], node_types: Dict[str, str],
                            connections: List[dict],
                            report: PreflightReport) -> None:
    """Validate the port contract of serialized connections.

    ``NodeGraph.connect()`` rejects malformed edges while editing, but a
    project can also be imported from an older version or authored by an
    external tool.  Preflight must therefore validate the serialized graph
    independently of the live graph's current connection list.  Port schemas
    are obtained from the registered node classes (or an explicit serialized
    schema when a plugin provides one), so this remains a pure, no-GL check.

    Group boundaries are flattened before this pass.  Invalid group mappings
    are reported by ``_check_groups`` and unresolved group endpoints are left
    alone here to avoid hiding the more useful mapping diagnostic.
    """
    node_data = {
        str(nd.get("id", "")): nd for nd in nodes
        if isinstance(nd, dict) and str(nd.get("id", ""))
    }
    schemas: Dict[str, Optional[Tuple[Dict[str, str], Dict[str, str]]]] = {}

    def schema_for(node_id: str) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
        if node_id in schemas:
            return schemas[node_id]
        nd = node_data.get(node_id)
        if nd is None:
            schemas[node_id] = None
            return None
        schema = _serialized_port_schema(nd)
        if schema is None:
            schema = _registered_port_schema(
                str(nd.get("type", node_types.get(node_id, ""))),
                nd.get("parameters") or {}, node_id)
        schemas[node_id] = schema
        return schema

    for conn in connections:
        src_id = str(conn.get("src_node_id", ""))
        dst_id = str(conn.get("dst_node_id", ""))
        if src_id not in node_types or dst_id not in node_types:
            # The dangling-endpoint pass owns this diagnostic.
            continue

        src_schema = schema_for(src_id)
        dst_schema = schema_for(dst_id)
        if src_schema is None or dst_schema is None:
            # Unknown/plugin nodes without a declared schema are already
            # covered by node.unknown_type; do not invent a port failure.
            continue

        src_port = str(conn.get("src_port_name", ""))
        dst_port = str(conn.get("dst_port_name", ""))
        src_inputs, src_outputs = src_schema
        dst_inputs, dst_outputs = dst_schema

        source_valid = src_port in src_outputs
        if not source_valid:
            code = ("connection.source_wrong_direction"
                    if src_port in src_inputs else "connection.source_port_missing")
            report.issues.append(PreflightIssue(
                code=code, severity=SEVERITY_ERROR,
                message=(f"连接的源节点 {src_id!r} 没有输出端口 {src_port!r}。"),
                node_id=src_id,
                suggestion="选择该节点的输出端口，或删除失效连接。"))

        destination_valid = dst_port in dst_inputs
        if not destination_valid:
            code = ("connection.destination_wrong_direction"
                    if dst_port in dst_outputs
                    else "connection.destination_port_missing")
            report.issues.append(PreflightIssue(
                code=code, severity=SEVERITY_ERROR,
                message=(f"连接的目标节点 {dst_id!r} 没有输入端口 {dst_port!r}。"),
                node_id=dst_id,
                suggestion="选择该节点的输入端口，或删除失效连接。"))

        # Without both endpoint types there is no meaningful type comparison.
        if not source_valid or not destination_valid:
            continue

        src_type = src_outputs[src_port]
        dst_type = dst_inputs[dst_port]
        if not _port_types_compatible(src_type, dst_type):
            report.issues.append(PreflightIssue(
                code="connection.type_mismatch", severity=SEVERITY_ERROR,
                message=(f"连接类型不兼容：{src_id}.{src_port} ({src_type}) "
                         f"→ {dst_id}.{dst_port} ({dst_type})。"),
                node_id=dst_id,
                suggestion="连接相同数据类型的端口，或增加转换节点。"))


def _serialized_port_schema(
        node_data: dict) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
    """Read an optional plugin-provided port schema from serialized data."""
    raw_inputs = node_data.get("input_ports")
    raw_outputs = node_data.get("output_ports")
    if raw_inputs is None and raw_outputs is None:
        return None

    def parse(raw: object) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if isinstance(raw, dict):
            for name, data_type in raw.items():
                result[str(name)] = str(data_type or "image")
            return result
        if not isinstance(raw, (list, tuple)):
            return result
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name")
                data_type = item.get("data_type", item.get("type", "image"))
                if name:
                    result[str(name)] = str(data_type or "image")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result[str(item[0])] = str(item[1] or "image")
            elif isinstance(item, str):
                result[item] = "image"
        return result

    return parse(raw_inputs), parse(raw_outputs)


def _registered_port_schema(
        node_type: str, parameters: dict, node_id: str
        ) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
    """Resolve a built-in node's ports without constructing a render object."""
    try:
        # Lazy import avoids making the graph registry a module-import cycle.
        from .node_engine import NODE_REGISTRY
        node_cls = NODE_REGISTRY.get(node_type)
        if node_cls is None:
            return None
        node = node_cls(node_id=node_id)
        # SwitchNode's input ports are parameter-driven.  Reapply its saved
        # count so a valid input15 edge is accepted after deserialization.
        if node_type == "switch" and "input_count" in parameters:
            node.set_parameter("input_count", parameters.get("input_count"))
        return (
            {str(port.name): str(port.data_type) for port in node.inputs},
            {str(port.name): str(port.data_type) for port in node.outputs},
        )
    except Exception:
        # A third-party node constructor may require optional dependencies.
        # Its capability/unknown-node diagnostic remains actionable; port
        # preflight must not make the whole report crash.
        return None


def _port_types_compatible(source_type: str, destination_type: str) -> bool:
    """Return whether a serialized output can feed a serialized input."""
    source = str(source_type or "image").lower()
    destination = str(destination_type or "image").lower()
    return source == destination or source == "any" or destination == "any"


def _group_target(value: object) -> Optional[tuple[str, str]]:
    """Normalize a serialized group mapping endpoint."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    node_id, port_name = str(value[0] or ""), str(value[1] or "")
    if not node_id or not port_name:
        return None
    return node_id, port_name


def _group_mapping_value(groups: Dict[str, dict], group_id: str,
                         mapping_name: str, port_name: str) -> object:
    """Return the raw mapping entry for a group port, or ``None``.

    AUD-48002: ``_check_groups`` diagnoses malformed mapping payloads
    (non-dict ``input_mappings``/``output_mappings``), but the boundary
    cycle walk and ``_flatten_group_connections`` must tolerate the same
    payloads instead of crashing on ``.get`` of a non-dict.
    """
    info = groups.get(group_id)
    if not isinstance(info, dict):
        return None
    mappings = info.get(mapping_name)
    if not isinstance(mappings, dict):
        return None
    return mappings.get(port_name)


def _group_info(nodes: List[dict]) -> Dict[str, dict]:
    """Return the serialized GroupNode metadata indexed by node ID."""
    result: Dict[str, dict] = {}
    for nd in nodes:
        if str(nd.get("type", "")) != "group":
            continue
        nid = str(nd.get("id", ""))
        result[nid] = {
            "members": [str(value) for value in (nd.get("member_node_ids") or [])],
            "inputs": nd.get("input_mappings") or {},
            "outputs": nd.get("output_mappings") or {},
        }
    return result


def _check_groups(nodes: List[dict], node_ids: Set[str],
                  report: PreflightReport) -> None:
    """Validate GroupNode membership and boundary mappings.

    Group mappings are intentionally validated from serialized data only, so
    export preflight stays free of Qt/GL and works for projects loaded on a
    machine without the original UI session.
    """
    groups = _group_info(nodes)
    for group_id, info in sorted(groups.items()):
        members = info["members"]
        if len(members) != len(set(members)):
            report.issues.append(PreflightIssue(
                code="group.duplicate_member", severity=SEVERITY_ERROR,
                message="Group 包含重复成员节点。", node_id=group_id,
                suggestion="删除重复成员后重新保存合成图。"))
        for member_id in members:
            if member_id not in node_ids:
                report.issues.append(PreflightIssue(
                    code="group.missing_member", severity=SEVERITY_ERROR,
                    message=f"Group 引用了不存在的成员节点 {member_id!r}。",
                    node_id=group_id,
                    suggestion="移除失效成员或恢复对应节点。"))

        for mapping_name, label in (("inputs", "输入"), ("outputs", "输出")):
            mappings = info[mapping_name]
            if not isinstance(mappings, dict):
                report.issues.append(PreflightIssue(
                    code="group.invalid_mapping", severity=SEVERITY_ERROR,
                    message=f"Group 的{label}端口映射格式无效。",
                    node_id=group_id,
                    suggestion="重新创建该 Group 端口映射。"))
                continue
            for port_name, raw_target in mappings.items():
                target = _group_target(raw_target)
                target_id = str(raw_target[0]) if isinstance(
                    raw_target, (tuple, list)) and raw_target else ""
                if target is None or target_id not in node_ids:
                    report.issues.append(PreflightIssue(
                        code="group.invalid_mapping", severity=SEVERITY_ERROR,
                        message=f"Group 的{label}端口 {port_name!r} 映射无效。",
                        node_id=group_id,
                        suggestion="检查成员节点和端口映射。"))
                    continue
                if target_id not in members:
                    report.issues.append(PreflightIssue(
                        code="group.mapping_outside_members", severity=SEVERITY_ERROR,
                        message=f"Group 的{label}端口映射指向组外节点 {target_id!r}。",
                        node_id=group_id,
                        suggestion="将目标节点加入 Group 或重新映射端口。"))

    # A group may contain another group. Membership cycles are not
    # representable as a finite macro expansion and must be rejected.
    membership_state: Dict[str, int] = {}

    def visit(group_id: str, path: List[str]) -> None:
        state = membership_state.get(group_id, 0)
        if state == 1:
            report.issues.append(PreflightIssue(
                code="group.membership_cycle", severity=SEVERITY_ERROR,
                message="Group 的嵌套成员存在循环。", node_id=group_id,
                suggestion="解除循环嵌套后再渲染。"))
            return
        if state == 2:
            return
        membership_state[group_id] = 1
        for member_id in groups[group_id]["members"]:
            if member_id in groups:
                visit(member_id, path + [group_id])
        membership_state[group_id] = 2

    for group_id in sorted(groups):
        visit(group_id, [])

    # Mapping cycles are distinct from membership cycles: they can occur when
    # one group's boundary points at another group's boundary port.
    for mapping_name in ("inputs", "outputs"):
        for group_id, info in sorted(groups.items()):
            for port_name, raw_target in (info[mapping_name].items()
                                          if isinstance(info[mapping_name], dict)
                                          else ()):
                current_id, current_port = group_id, str(port_name)
                visited: Set[tuple[str, str]] = set()
                while current_id in groups:
                    marker = (current_id, current_port)
                    if marker in visited:
                        report.issues.append(PreflightIssue(
                            code="group.boundary_cycle", severity=SEVERITY_ERROR,
                            message="Group 端口映射存在循环，无法展开子图边界。",
                            node_id=group_id,
                            suggestion="修改互相引用的 Group 端口映射。"))
                        break
                    visited.add(marker)
                    target = _group_target(
                        _group_mapping_value(
                            groups, current_id, mapping_name, current_port))
                    if target is None:
                        break
                    current_id, current_port = target


def _flatten_group_connections(nodes: List[dict], connections: List[dict]) -> List[dict]:
    """Flatten valid GroupNode boundary edges for topology diagnostics."""
    groups = _group_info(nodes)

    def resolve(node_id: str, port_name: str, mapping_name: str) -> tuple[str, str]:
        current_id, current_port = str(node_id), str(port_name)
        visited: Set[tuple[str, str]] = set()
        while current_id in groups:
            marker = (current_id, current_port)
            if marker in visited:
                return str(node_id), str(port_name)
            visited.add(marker)
            target = _group_target(
                _group_mapping_value(groups, current_id, mapping_name,
                                     current_port))
            if target is None:
                return current_id, current_port
            current_id, current_port = target
        return current_id, current_port

    flattened: List[dict] = []
    seen: Set[tuple[str, str, str, str]] = set()
    for conn in connections:
        src_id, src_port = resolve(
            str(conn.get("src_node_id", "")),
            str(conn.get("src_port_name", "")), "outputs")
        dst_id, dst_port = resolve(
            str(conn.get("dst_node_id", "")),
            str(conn.get("dst_port_name", "")), "inputs")
        key = (src_id, src_port, dst_id, dst_port)
        if key in seen:
            continue
        seen.add(key)
        flattened.append({
            "src_node_id": src_id,
            "src_port_name": src_port,
            "dst_node_id": dst_id,
            "dst_port_name": dst_port,
        })
    return flattened


def _check_expressions(nodes: List[dict], node_ids: Set[str],
                       report: PreflightReport) -> None:
    """Validate serialized expressions before a render can consume them.

    The live expression API validates syntax when an expression is edited,
    but projects can also be imported, migrated, or authored by external
    tools.  This pass therefore works directly on serialized data and checks
    the references that the runtime context can actually resolve.  It keeps
    graph preflight deterministic and avoids evaluating user code while
    reporting errors.
    """
    node_by_id = {
        str(nd.get("id", "")): nd for nd in nodes
        if isinstance(nd, dict) and str(nd.get("id", ""))
    }
    aliases = {f"n_{node_id}": node_id for node_id in node_by_id}
    parser = ExpressionParser()
    expression_keys: Set[tuple[str, str]] = set()
    dependency_edges: Dict[tuple[str, str], Set[tuple[str, str]]] = {}

    for node_id, node_data in sorted(node_by_id.items()):
        expressions = node_data.get("expressions") or {}
        if not isinstance(expressions, dict):
            report.issues.append(PreflightIssue(
                code="expression.invalid_payload", severity=SEVERITY_ERROR,
                message="节点的表达式数据格式无效。", node_id=node_id,
                suggestion="删除损坏的表达式数据后重新保存项目。"))
            continue
        parameters = node_data.get("parameters") or {}
        if not isinstance(parameters, dict):
            parameters = {}
        known_parameters = {str(name) for name in parameters}
        enabled_map = node_data.get("expression_enabled") or {}
        if not isinstance(enabled_map, dict):
            enabled_map = {}

        for raw_param_name, expression in sorted(expressions.items(),
                                                 key=lambda item: str(item[0])):
            param_name = str(raw_param_name)
            # Disabled expressions remain persisted for later re-enabling,
            # but do not participate in the current render contract.  This
            # makes the serialized preflight match snapshot evaluation.
            if enabled_map.get(param_name, True) is False:
                continue
            key = (node_id, param_name)
            expression_keys.add(key)
            if param_name not in known_parameters:
                report.issues.append(PreflightIssue(
                    code="expression.param_not_found", severity=SEVERITY_ERROR,
                    message=f"表达式引用了不存在的本节点参数 {param_name!r}。",
                    node_id=node_id,
                    suggestion="将表达式绑定到有效参数，或删除该表达式。"))

            if not isinstance(expression, str):
                report.issues.append(PreflightIssue(
                    code="expression.invalid_payload", severity=SEVERITY_ERROR,
                    message=f"参数 {param_name!r} 的表达式不是字符串。",
                    node_id=node_id,
                    suggestion="将表达式改为受支持的文本表达式。"))
                continue

            result = parser.validate(expression)
            if not result.success:
                detail = result.error.message if result.error else "表达式无效"
                report.issues.append(PreflightIssue(
                    code="expression.syntax_error", severity=SEVERITY_ERROR,
                    message=f"表达式 {param_name!r} 无法解析：{detail}。",
                    node_id=node_id,
                    suggestion="修正语法或移除该表达式。"))
                continue

            edges: Set[tuple[str, str]] = set()
            for local_name in parser.extract_local_parameter_names(expression):
                if local_name not in known_parameters:
                    report.issues.append(PreflightIssue(
                        code="expression.param_not_found", severity=SEVERITY_ERROR,
                        message=(f"表达式引用了不存在的本节点参数 "
                                 f"{local_name!r}。"),
                        node_id=node_id,
                        suggestion="检查 this.<参数名> 是否拼写正确。"))
                edges.add((node_id, local_name))

            for dependency in result.dependencies:
                if not dependency.node_id:
                    continue
                target_id = aliases.get(dependency.node_id,
                                         dependency.node_id)
                if target_id not in node_by_id:
                    report.issues.append(PreflightIssue(
                        code="expression.node_not_found", severity=SEVERITY_ERROR,
                        message=(f"表达式引用了不存在的节点 "
                                 f"{dependency.node_id!r}。"),
                        node_id=node_id,
                        suggestion="重新绑定到现有节点，或删除该引用。"))
                    continue

                target_data = node_by_id[target_id]
                target_params = target_data.get("parameters") or {}
                target_names = ({str(name) for name in target_params}
                                if isinstance(target_params, dict) else set())
                if dependency.param_name not in target_names:
                    report.issues.append(PreflightIssue(
                        code="expression.param_not_found", severity=SEVERITY_ERROR,
                        message=(f"表达式引用了不存在的参数 "
                                 f"{dependency.node_id}.{dependency.param_name}。"),
                        node_id=node_id,
                        suggestion="检查节点参数名，或重新绑定表达式。"))
                edges.add((target_id, dependency.param_name))
            dependency_edges[key] = edges

    # Detect cycles only after all expressions have been scanned, so a
    # missing external node cannot be mistaken for a circular dependency.
    # Checking reachability back to *each* start node deliberately reports all
    # members of a strongly connected component, including a component that
    # also contains a direct self-reference.
    def reaches(start: tuple[str, str], target: tuple[str, str]) -> bool:
        pending = [
            dependency for dependency in dependency_edges.get(start, set())
            if dependency in expression_keys
        ]
        visited: Set[tuple[str, str]] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(
                dependency for dependency in dependency_edges.get(current, set())
                if dependency in expression_keys and dependency not in visited
            )
        return False

    cycle_keys = {
        key for key in expression_keys
        if reaches(key, key)
    }
    for node_id, param_name in sorted(cycle_keys):
        report.issues.append(PreflightIssue(
            code="expression.circular_dependency", severity=SEVERITY_ERROR,
            message=f"表达式 {param_name!r} 参与了循环依赖。", node_id=node_id,
            suggestion="打断表达式之间的循环引用。"))


def _check_precomps(nodes: List[dict], graph_data: dict,
                    node_ids: Set[str], report: PreflightReport) -> None:
    """Validate nested LayerStack references and detect stack recursion."""
    stacks = graph_data.get("layer_stacks") or {}
    layers = graph_data.get("layers") or {}
    if not isinstance(stacks, dict):
        stacks = {}
    if not isinstance(layers, dict):
        layers = {}

    node_by_id = {str(nd.get("id", "")): nd for nd in nodes}
    precomp_nodes = {
        nid: nd for nid, nd in node_by_id.items()
        if str(nd.get("type", "")) == "precomp"
    }
    layer_owner: Dict[str, str] = {}
    for stack_id, stack in stacks.items():
        if not isinstance(stack, dict):
            continue
        for layer_id in stack.get("layer_ids") or []:
            layer_owner[str(layer_id)] = str(stack_id)

    stack_edges: Dict[str, str] = {}
    for node_id, node_data in sorted(precomp_nodes.items()):
        params = node_data.get("parameters") or {}
        child_id = str(params.get("child_stack_id", "") or "")
        if not child_id:
            report.issues.append(PreflightIssue(
                code="precomp.missing_child_stack", severity=SEVERITY_ERROR,
                message="Precomp 没有绑定子合成堆栈。", node_id=node_id,
                suggestion="创建或重新绑定一个子合成。"))
            continue
        child_stack = stacks.get(child_id)
        if not isinstance(child_stack, dict):
            report.issues.append(PreflightIssue(
                code="precomp.missing_child_stack", severity=SEVERITY_ERROR,
                message=f"Precomp 引用了不存在的子堆栈 {child_id!r}。",
                node_id=node_id,
                suggestion="恢复该子合成或删除失效 Precomp。"))
            continue

        child_output = str(child_stack.get("output_node_id", "") or "")
        if child_output != node_id:
            report.issues.append(PreflightIssue(
                code="precomp.output_mismatch", severity=SEVERITY_ERROR,
                message="子合成输出边界没有指向对应的 Precomp 节点。",
                node_id=node_id,
                suggestion="将子堆栈 output_node_id 修复为该 Precomp。"))

        parent_stack = ""
        for layer_id, layer in layers.items():
            if isinstance(layer, dict) and str(layer.get("root_node_id", "")) == node_id:
                parent_stack = layer_owner.get(str(layer_id), "")
                break
        if parent_stack and parent_stack != child_id:
            stack_edges[parent_stack] = child_id

    # DFS over parent-stack -> child-stack references. A cycle would make
    # recursive compilation and cache invalidation non-terminating.
    state: Dict[str, int] = {}

    def visit(stack_id: str) -> None:
        current = state.get(stack_id, 0)
        if current == 1:
            report.issues.append(PreflightIssue(
                code="precomp.stack_cycle", severity=SEVERITY_ERROR,
                message="嵌套合成堆栈存在循环引用。", node_id=stack_id,
                suggestion="解除子合成之间的循环引用。"))
            return
        if current == 2:
            return
        state[stack_id] = 1
        child_id = stack_edges.get(stack_id)
        if child_id in stacks:
            visit(child_id)
        state[stack_id] = 2

    for stack_id in sorted(stacks):
        visit(str(stack_id))


def _check_layer_chain_conflicts(graph_data: dict, node_ids: Set[str],
                                 connections: List[dict],
                                 report: PreflightReport) -> None:
    """Report managed Merge inputs that were authored outside the layer chain."""
    stacks = graph_data.get("layer_stacks") or {}
    layers = graph_data.get("layers") or {}
    if not isinstance(stacks, dict) or not isinstance(layers, dict):
        return
    for stack_id, stack in sorted(stacks.items()):
        if not isinstance(stack, dict) or not stack.get("managed", True):
            continue
        generated = {str(value) for value in
                     (stack.get("generated_merge_node_ids") or [])}
        for merge_id in sorted(generated):
            if merge_id not in node_ids:
                report.issues.append(PreflightIssue(
                    code="layer.generated_node_missing", severity=SEVERITY_ERROR,
                    message=f"LayerStack 引用了不存在的自动 Merge 节点 {merge_id!r}。",
                    node_id=merge_id,
                    suggestion="重建 LayerStack 的自动合成链。"))
        roots = {
            str(layers[layer_id].get("root_node_id", ""))
            for layer_id in (stack.get("layer_ids") or [])
            if isinstance(layers.get(layer_id), dict)
        }
        allowed = roots | generated
        for conn in connections:
            dst = str(conn.get("dst_node_id", ""))
            port = str(conn.get("dst_port_name", ""))
            src = str(conn.get("src_node_id", ""))
            if dst in generated and port in {"A", "B"} and src not in allowed:
                report.issues.append(PreflightIssue(
                    code="layer.managed_chain_conflict", severity=SEVERITY_WARNING,
                    message=(f"LayerStack {stack_id!r} 的自动 Merge 输入被组外节点"
                             f" {src!r} 手动接管。"),
                    node_id=dst,
                    suggestion="确认后重建自动链，或将该 LayerStack 切换为自由图。"))


def _check_layers(layers: dict, node_ids: Set[str],
                  report: PreflightReport) -> None:
    for lid, ld in (layers or {}).items():
        for field_name in ("root_node_id", "source_node_id"):
            ref = str((ld or {}).get(field_name, "") or "")
            if ref and ref not in node_ids:
                report.issues.append(PreflightIssue(
                    code="layer.invalid_reference", severity=SEVERITY_WARNING,
                    message=f"图层引用了不存在的节点 ({field_name}={ref!r})。",
                    node_id=ref, suggestion="重建该图层或重新指定其来源。"))
        in_f = (ld or {}).get("in_frame")
        out_f = (ld or {}).get("out_frame")
        if (isinstance(in_f, int) and isinstance(out_f, int)
                and in_f > out_f):
            report.issues.append(PreflightIssue(
                code="layer.time_range_inverted", severity=SEVERITY_WARNING,
                message=f"图层入点 ({in_f}) 大于出点 ({out_f})。",
                suggestion="修正图层的入/出点。"))
        matte = str((ld or {}).get("track_matte_layer_id", "") or "")
        if matte and matte not in layers:
            report.issues.append(PreflightIssue(
                code="layer.track_matte_missing", severity=SEVERITY_WARNING,
                message=f"轨道遮罩引用了不存在的图层 {matte!r}。",
                suggestion="重新指定遮罩来源图层。"))
        if matte and matte == str(lid):
            report.issues.append(PreflightIssue(
                code="layer.track_matte_self", severity=SEVERITY_ERROR,
                message="图层不能将自己作为轨道遮罩。", node_id=str(lid),
                suggestion="选择另一个图层作为遮罩来源。"))
        mode = str((ld or {}).get("track_matte_mode", "alpha") or "alpha")
        if mode not in {"alpha", "luma"}:
            report.issues.append(PreflightIssue(
                code="layer.track_matte_mode_invalid", severity=SEVERITY_WARNING,
                message=f"轨道遮罩模式 {mode!r} 不受支持。", node_id=str(lid),
                suggestion="使用 alpha 或 luma 模式。"))


def _check_track_mattes(layers: dict, stacks: dict,
                        report: PreflightReport) -> None:
    """Validate matte ownership and detect indirect matte cycles."""
    if not isinstance(layers, dict):
        return
    owner: Dict[str, str] = {}
    if isinstance(stacks, dict):
        for stack_id, stack in stacks.items():
            if not isinstance(stack, dict):
                continue
            for layer_id in stack.get("layer_ids") or []:
                owner[str(layer_id)] = str(stack_id)

    for layer_id, layer in layers.items():
        if not isinstance(layer, dict):
            continue
        matte_id = str(layer.get("track_matte_layer_id", "") or "")
        if not matte_id or matte_id not in layers:
            continue
        target_stack = owner.get(str(layer_id))
        matte_stack = owner.get(matte_id)
        if target_stack and matte_stack and target_stack != matte_stack:
            report.issues.append(PreflightIssue(
                code="layer.track_matte_external", severity=SEVERITY_WARNING,
                message="轨道遮罩来源不在目标图层的同一 LayerStack 中。",
                node_id=str(layer_id),
                suggestion="将遮罩移入同一堆栈，或改用节点图连接。"))

    state: Dict[str, int] = {}

    def visit(layer_id: str) -> None:
        current = state.get(layer_id, 0)
        if current == 1:
            report.issues.append(PreflightIssue(
                code="layer.track_matte_cycle", severity=SEVERITY_ERROR,
                message="轨道遮罩关系存在循环引用。", node_id=layer_id,
                suggestion="解除循环遮罩关系后再渲染。"))
            return
        if current == 2:
            return
        state[layer_id] = 1
        layer = layers.get(layer_id)
        matte_id = (str(layer.get("track_matte_layer_id", "") or "")
                    if isinstance(layer, dict) else "")
        if matte_id in layers:
            visit(matte_id)
        state[layer_id] = 2

    for layer_id in sorted(layers):
        visit(str(layer_id))


def run_preflight(graph, mode: str = MODE_PREVIEW,
                  check_fs: bool = False,
                  project_dir: Optional[str] = None,
                  asset_search_roots: Iterable[str] = ()) -> PreflightReport:
    """Preflight a live :class:`NodeGraph` (serialized internally)."""
    return run_preflight_data(
        graph.to_dict(), mode=mode, check_fs=check_fs,
        project_dir=project_dir, asset_search_roots=asset_search_roots)
