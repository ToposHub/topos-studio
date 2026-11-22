"""Resolve external Precomp nodes into worker-owned evaluation snapshots."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from fractions import Fraction
import posixpath
from pathlib import Path
from threading import Lock
from typing import Any
from math import floor

from src.shared.project.resource_resolver import (
    ProjectResourceResolver, ResourceResolutionError, sha256_file,
)
from src.shared.project.topos_project_io import MAX_COMPOSITE_ARCHIVE_BYTES
from src.shared.project.topos_project_read_service import ToposProjectReadService

from ..core.external_comp_refs import (
    CompKey, CompRef, read_comp_ref,
    DEFAULT_MAX_DEPTH, DEFAULT_MAX_NODES,
    REF_CYCLE, REF_DEPTH_EXCEEDED, REF_INVALID, REF_MISSING_FILE,
    REF_MISSING_OUTPUT, REF_PLUGIN_MISMATCH, REF_PROJECT_MISMATCH,
    REF_REVISION_MISMATCH, REF_SIZE_EXCEEDED, UPDATE_POLICY_PINNED,
    REF_UNSUPPORTED_CONTEXT,
)
from ..core.graph_snapshot import ConnectionSnapshot, GraphSnapshot, NodeSnapshot
from ..core.layer_model import EMPTY_LAYER_OUTPUT_PARAM
from ..core.layer_time import fps_fraction
from ..core.output_targets import (
    normalize_output_name, output_name_for_node, selected_output_dependency_ids,
)
from ..core.time_remap import TimeRemap, gate_active, remap_for_parameters

_MAX_PACKAGES = 32
EXTERNAL_ARCHIVE_ROLE = "external_composition_archive_v1"

# Archive only graph branches whose complete media dependencies we can inspect.
# Imported 3D models, image sequences and installed plugins can discover more
# files after initialization and therefore remain explicit linked dependencies.
_HANDOFF_PORTABLE_NODES = frozenset({
    "audio_source", "blur", "channel_merge", "clamp", "color_correct", "constant",
    "copy_channel", "crop", "cs_transform", "dissolve", "dot", "frame_hold",
    "grade", "group", "huecorrect", "image_input", "invert", "layer_style", "lut",
    "merge", "noop", "output", "precomp", "premult", "reformat", "remove_channels",
    "retime", "set_alpha", "shuffle", "switch", "time_offset", "transform",
    "unpremult", "video_frame",
})


class ReferenceLoadError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class _Archive:
    signature: tuple[int, int, int, int, int]
    revision: str
    project_id: str
    data: dict[str, Any]


@dataclass
class _Branch:
    archive_path: Path
    snapshot: GraphSnapshot
    node_ids: dict[str, str]
    pending: Iterator[NodeSnapshot]
    active_path: tuple[CompKey, ...] = ()
    revision: str = ""
    root_node_id: str = ""
    package_resolver: ProjectResourceResolver | None = None


@dataclass(frozen=True)
class ExternalArchiveLink:
    """One checked edge used to assemble a handoff's archive resources."""

    root_node_id: str
    parent_revision: str
    relative_path: str
    local_path: str
    archive_revision: str
    embedded: bool
    portable: bool


def _relative_archive_key(path: str) -> str:
    # This value is a lookup key. ResourceResolver checks the actual archive
    # member path, independently of the project-relative reference.
    return posixpath.normpath(path.replace("\\", "/"))


def _embedded_archive(
    resolver: ProjectResourceResolver | None, parent_revision: str, ref: CompRef,
) -> Path | None:
    if resolver is None:
        return None
    key = [parent_revision, _relative_archive_key(ref.relative_path)]
    for entry in resolver.index.entries_for_role(EXTERNAL_ARCHIVE_ROLE):
        edges = entry.metadata.get("reference_edges")
        if not isinstance(edges, list) or key not in edges:
            continue
        if (ref.update_policy == UPDATE_POLICY_PINNED and ref.revision_fingerprint
                and ref.revision_fingerprint != entry.sha256):
            continue
        return Path(resolver.resolve_uri(entry.uri).local_path)
    return None


def _archive_owns_selected_media(
    nodes: dict[str, NodeSnapshot], resolver: ProjectResourceResolver | None,
) -> bool:
    from ..core.asset_dependency import collect_asset_dependencies

    if any(node.node_type not in _HANDOFF_PORTABLE_NODES for node in nodes.values()):
        return False
    references = collect_asset_dependencies({
        "nodes": [
            {"id": node.node_id, "type": node.node_type,
             "parameters": node.parameters, "disabled": node.disabled}
            for node in nodes.values()
        ],
    }, check_fs=False).references
    if not references:
        return True
    if resolver is None:
        return False
    required_paths = {item.stored_path for item in references}
    for entry in resolver.iter_entries():
        resolved_path = resolver.local_path_for(entry)
        if required_paths.intersection({resolved_path, entry.uri, entry.content_uri}):
            resolver.resolve_uri(entry.uri)
            required_paths.difference_update({resolved_path, entry.uri, entry.content_uri})
    return not required_paths


def _references(
    nodes: dict[str, NodeSnapshot], dependency_ids: frozenset[str] | None,
) -> Iterator[NodeSnapshot]:
    for node in nodes.values():
        if (node.node_type == "precomp" and not node.disabled
                and (dependency_ids is None or node.node_id in dependency_ids)):
            yield node


def _has_reference_fields(node: NodeSnapshot) -> bool:
    if node.node_type != "precomp":
        return False
    structured_ref = node.parameters.get("external_comp_ref")
    if structured_ref is not None:
        return structured_ref != {}
    return any(node.parameters.get(key) for key in (
        "ref_project_id", "ref_comp_id",
        "ref_relative_path", "ref_revision_fingerprint",
    ))


def has_external_references(snapshot: GraphSnapshot) -> bool:
    return any(_has_reference_fields(node) for node in getattr(snapshot, "nodes", {}).values())


class ExternalCompSnapshots:
    """Reuse loaded read-only packages across instances and render requests."""

    def __init__(self, *, freeze: bool = False) -> None:
        self.freeze = freeze
        self._packages: OrderedDict[Path, _Archive] = OrderedDict()
        self._graphs: dict[tuple[Path, str, str], tuple[Any, Fraction, str]] = {}
        self._reader = ToposProjectReadService()
        self._lock = Lock()

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int, int, int]:
        try:
            info = path.stat()
        except OSError as exc:
            raise ReferenceLoadError(REF_MISSING_FILE, str(path)) from exc
        if info.st_size > MAX_COMPOSITE_ARCHIVE_BYTES:
            raise ReferenceLoadError(REF_SIZE_EXCEEDED, str(path))
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _archive(self, path: Path) -> _Archive:
        cached = self._packages.get(path)
        if self.freeze and cached is not None:
            return cached
        signature = self._signature(path)
        if cached is not None and cached.signature == signature:
            self._packages.move_to_end(path)
            return cached
        if len(self._packages) >= _MAX_PACKAGES and cached is None:
            if self.freeze:
                raise ReferenceLoadError(REF_SIZE_EXCEEDED, "Too many linked packages")
            expired, _ = self._packages.popitem(last=False)
            self._graphs = {key: graph for key, graph in self._graphs.items() if key[0] != expired}
        try:
            revision = sha256_file(str(path))
            bundle = self._reader.load_composites(str(path), revision=revision)
        except (OSError, ValueError, TypeError) as exc:
            raise ReferenceLoadError(REF_INVALID, str(exc)) from exc
        if bundle is None or signature != self._signature(path):
            raise ReferenceLoadError(REF_INVALID, f"Package unreadable or changed while loading: {path}")
        archive = _Archive(signature, revision, *bundle)
        self._packages[path] = archive
        self._packages.move_to_end(path)
        self._graphs = {key: graph for key, graph in self._graphs.items() if key[0] != path}
        return archive

    def _graph(self, path: Path, ref: CompRef) -> tuple[Any, Fraction, str]:
        from ..core.node_engine import NODE_REGISTRY, NodeGraph

        archive = self._archive(path)
        if archive.project_id != ref.project_id:
            raise ReferenceLoadError(REF_PROJECT_MISMATCH, archive.project_id)
        if (ref.update_policy == UPDATE_POLICY_PINNED and ref.revision_fingerprint
                and archive.revision != ref.revision_fingerprint):
            raise ReferenceLoadError(REF_REVISION_MISMATCH, archive.revision)
        key = path, archive.revision, ref.comp_id
        cached = self._graphs.get(key)
        if cached is not None:
            return cached
        compositions = archive.data.get("compositions")
        item = compositions.get(ref.comp_id) if isinstance(compositions, dict) else None
        if not isinstance(item, dict):
            raise ReferenceLoadError(REF_MISSING_OUTPUT, ref.comp_id)
        raw_nodes = item.get("nodes", [])
        if not isinstance(raw_nodes, list) or len(raw_nodes) > DEFAULT_MAX_NODES:
            raise ReferenceLoadError(REF_SIZE_EXCEEDED, ref.comp_id)
        unknown = {str(node.get("type")) for node in raw_nodes if isinstance(node, dict)
                   and node.get("type") not in NODE_REGISTRY}
        if unknown:
            raise ReferenceLoadError(REF_PLUGIN_MISMATCH, ", ".join(sorted(unknown)))
        graph = NodeGraph.from_dict(item)
        assets = archive.data.get("assets")
        assets = assets if isinstance(assets, dict) else {}
        resolver = archive.data.get("resource_resolver")
        for node in graph.nodes.values():
            params = node.parameters
            asset = assets.get(str(params.get("asset_id", "")))
            if isinstance(asset, dict) and asset.get("path") and "file_path" in params:
                params["file_path"] = asset["path"]
            for name in ("file_path", "lut_path", "model_path", "image_path"):
                value = params.get(name)
                if not isinstance(value, str) or not value:
                    continue
                if value.startswith("topos-res://"):
                    try:
                        resolved = resolver.resolve_uri(value) if resolver is not None else None
                    except ResourceResolutionError as exc:
                        raise ReferenceLoadError(REF_MISSING_FILE, value) from exc
                    if resolved is None or not resolved.local_path:
                        raise ReferenceLoadError(REF_MISSING_FILE, value)
                    params[name] = resolved.local_path
                elif not Path(value).is_absolute():
                    params[name] = str(path.parent / value)
        fps = fps_fraction(item.get("fps", 24))
        if fps <= 0:
            raise ReferenceLoadError(REF_INVALID, "Invalid referenced frame rate")
        result = graph, fps, archive.revision
        self._graphs[key] = result
        return result

    def expand(
        self, snapshot: GraphSnapshot, *, output_name: str | None = None,
        output_node_id: str | None = None,
    ) -> GraphSnapshot:
        """Replace external inputs in an independent snapshot, preserving node ownership."""
        if getattr(snapshot, "reference_diagnostics", ()) or not has_external_references(snapshot):
            return snapshot
        with self._lock:
            return self._expand(snapshot, output_name, output_node_id)

    def inspect_archive_links(
        self, snapshot: GraphSnapshot,
    ) -> tuple[GraphSnapshot, tuple[ExternalArchiveLink, ...]]:
        if not has_external_references(snapshot):
            return snapshot, ()
        links: list[ExternalArchiveLink] = []
        with self._lock:
            expanded = self._expand(snapshot, None, None, archive_links=links)
        return expanded, tuple(links)

    def _linked_graph(
        self, logical_path: Path, parent: _Branch, ref: CompRef,
        root_resolver: ProjectResourceResolver | None, prefer_embedded: bool,
    ) -> tuple[Path, Any, Fraction, str, bool]:
        def embedded_path() -> Path | None:
            return (
                _embedded_archive(root_resolver, parent.revision, ref)
                or _embedded_archive(parent.package_resolver, "", ref)
            )

        embedded = embedded_path() if prefer_embedded else None
        path = embedded if embedded is not None else logical_path
        try:
            graph, fps, revision = self._graph(path, ref)
        except ReferenceLoadError as exc:
            if embedded is not None or exc.code not in {REF_MISSING_FILE, REF_REVISION_MISMATCH}:
                raise
            embedded = embedded_path()
            if embedded is None:
                raise
            path = embedded
            graph, fps, revision = self._graph(path, ref)
        return path, graph, fps, revision, embedded is not None

    def _expand(
        self, snapshot: GraphSnapshot, output_name: str | None,
        output_node_id: str | None,
        archive_links: list[ExternalArchiveLink] | None = None,
    ) -> GraphSnapshot:
        from ..core.node_engine import NodeGraph

        nodes = dict(snapshot.nodes)
        connections = list(snapshot.connections)
        gates = dict(snapshot.layer_frame_gates)
        inactive_layer_inputs = dict(snapshot.inactive_layer_inputs)
        remaps = dict(snapshot.frame_overrides)
        external_node_frames = dict(snapshot.external_node_frames)
        diagnostics = []
        host_path = Path(snapshot.project_archive_path) if snapshot.project_archive_path else Path()
        root_resolver = snapshot.external_archive_resolver
        dependencies = (selected_output_dependency_ids(
            snapshot.nodes.values(), snapshot.connections, output_name, output_node_id,
        ) if output_name is not None else None)
        stack = [_Branch(
            host_path, snapshot, {node_id: node_id for node_id in nodes},
            _references(snapshot.nodes, dependencies),
        )]
        while stack:
            parent = stack[-1]
            node = next(parent.pending, None)
            if node is None:
                stack.pop()
                continue
            if not _has_reference_fields(node):
                continue
            parent_node_id = parent.node_ids[node.node_id]
            connections = [conn for conn in connections
                           if (conn.dst_node_id, conn.dst_port_name) != (parent_node_id, "image")]
            try:
                ref = read_comp_ref(node.parameters)
                if ref is None or not ref.relative_path:
                    raise ReferenceLoadError(REF_INVALID, "Missing relative package location")
                if not snapshot.project_archive_path:
                    raise ReferenceLoadError(REF_MISSING_FILE, "Save the host project to resolve relative links")
                if ref.key in parent.active_path:
                    cycle = parent.active_path[parent.active_path.index(ref.key):] + (ref.key,)
                    raise ReferenceLoadError(
                        REF_CYCLE, " → ".join(f"{project}/{comp}" for project, comp in cycle))
                if len(stack) >= DEFAULT_MAX_DEPTH:
                    raise ReferenceLoadError(REF_DEPTH_EXCEEDED, ref.comp_id)
                relative_key = _relative_archive_key(ref.relative_path)
                logical_path = (parent.archive_path.parent / relative_key).resolve()
                child_path, graph, fps, revision, embedded = self._linked_graph(
                    logical_path, parent, ref, root_resolver, archive_links is not None,
                )
                parent_frame = parent.snapshot.frame_number
                override = parent.snapshot.frame_overrides.get(node.node_id)
                if override is not None:
                    parent_frame = override.apply(parent_frame)
                mapped_frame = remap_for_parameters("precomp", node.parameters)
                if mapped_frame is not None and mapped_frame.hold is not None:
                    frame = mapped_frame.hold
                else:
                    parent_fps = fps_fraction(parent.snapshot.fps)
                    if parent_fps <= 0:
                        raise ReferenceLoadError(REF_INVALID, "Invalid host frame rate")
                    speed = fps_fraction(mapped_frame.speed) if mapped_frame else Fraction(1)
                    offset = mapped_frame.offset if mapped_frame else 0
                    frame = floor(Fraction(parent_frame) * speed * fps / parent_fps) + offset
                child = graph.snapshot(frame_number=frame, fps=float(fps))
                structured_ref = node.parameters.get("external_comp_ref")
                output_name = normalize_output_name(
                    structured_ref.get("output_name") if isinstance(structured_ref, dict)
                    else node.parameters.get("ref_output_name")
                )
                output = next((ns for ns in child.nodes.values()
                               if ns.node_type == "output" and output_name_for_node(ns) == output_name), None)
                if output is None or (
                    not child.get_input_connections(output.node_id)
                    and not output.parameters.get(EMPTY_LAYER_OUTPUT_PARAM)
                ):
                    raise ReferenceLoadError(REF_MISSING_OUTPUT, output_name)
                child_dependencies = selected_output_dependency_ids(
                    child.nodes.values(), child.connections, output_name, output.node_id,
                )
                child_nodes = {nid: child_node for nid, child_node in child.nodes.items()
                               if child_dependencies is None or nid in child_dependencies}
                if any(ns.node_type in {"color_input", "timeline_clip_input"}
                       for ns in child_nodes.values()):
                    raise ReferenceLoadError(
                        REF_UNSUPPORTED_CONTEXT,
                        "External outputs require explicit media source nodes.",
                    )
                if len(nodes) + len(child_nodes) > DEFAULT_MAX_NODES:
                    raise ReferenceLoadError(REF_SIZE_EXCEEDED, ref.comp_id)
                resolver = self._packages[child_path].data.get("resource_resolver")
                root_node_id = parent.root_node_id or parent_node_id
                if archive_links is not None:
                    archive_links.append(ExternalArchiveLink(
                        root_node_id, parent.revision, relative_key, str(child_path), revision,
                        embedded, _archive_owns_selected_media(child_nodes, resolver),
                    ))
            except (ValueError, OSError, ResourceResolutionError) as exc:
                diagnostics.append({
                    "code": getattr(exc, "code", REF_INVALID),
                    "node_id": parent_node_id, "detail": str(exc),
                })
                continue

            node_ids = {nid: f"{parent_node_id}/ref/{revision[:16]}/{nid}" for nid in child_nodes}
            for nid, child_node in child_nodes.items():
                node_id = node_ids[nid]
                params = child_node.parameters
                if child_node.node_type == "output":
                    params = {**params, "output_name": node_id}
                nodes[node_id] = replace(
                    child_node, node_id=node_id, parameters=params,
                    param_revision=hash((child_node.param_revision, frame)),
                )
                child_remap = child.frame_overrides.get(nid)
                local_frame = child_remap.apply(frame) if child_remap is not None else frame
                remaps[node_id] = TimeRemap(hold=local_frame)
                external_node_frames[node_id] = local_frame
                gate = child.layer_frame_gates.get(nid)
                if gate is not None and not gate_active(gate, frame):
                    gates[node_id] = (snapshot.frame_number + 1, snapshot.frame_number + 1)
                if nid in child.inactive_layer_inputs:
                    inactive_layer_inputs[node_id] = child.inactive_layer_inputs[nid]
            connections.extend(
                replace(conn, src_node_id=node_ids[conn.src_node_id], dst_node_id=node_ids[conn.dst_node_id])
                for conn in child.connections
                if conn.src_node_id in node_ids and conn.dst_node_id in node_ids
            )
            connections.append(ConnectionSnapshot(
                node_ids[output.node_id], "display", parent_node_id, "image",
            ))
            stack.append(_Branch(
                logical_path, child, node_ids, _references(child_nodes, None),
                (*parent.active_path, ref.key), revision, root_node_id, resolver,
            ))
        return replace(
            snapshot, nodes=nodes, connections=connections,
            eval_order=NodeGraph._snapshot_topological_sort(list(nodes), connections),
            layer_frame_gates=gates, frame_overrides=remaps,
            inactive_layer_inputs=inactive_layer_inputs,
            reference_diagnostics=tuple(diagnostics),
            external_node_frames=external_node_frames,
        )
