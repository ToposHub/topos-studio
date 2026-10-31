"""Serialized composite asset dependency reporting.

This module is deliberately pure: it only inspects the serialized graph and
performs read-only path resolution.  The report is useful to preflight, an
offline-assets panel, and a future collect/relink command without coupling
those consumers to node implementations or the render thread.

The graph stores most direct dependencies as node parameters.  Material
textures embedded in an imported model are not available until the model is
parsed, but explicit ``material_overrides`` texture slots are serialized and
are therefore included here as well.
"""

from __future__ import annotations

import hashlib
import os
import json
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..render3d.format_capabilities import resolve_asset_path


STATUS_MISSING_PATH = "missing_path"
STATUS_UNVERIFIED = "unverified"
STATUS_MISSING = "missing"
STATUS_RESOLVED = "resolved"
STATUS_RELINK_CANDIDATE = "relink_candidate"

ROLE_MEDIA = "media"
ROLE_MODEL = "model"
ROLE_LUT = "lut"
ROLE_HDRI = "hdri"
ROLE_IES = "ies"
ROLE_MATERIAL_TEXTURE = "material_texture"


@dataclass(frozen=True)
class AssetReference:
    """One serialized path reference and its read-only resolution result."""

    node_id: str
    node_type: str
    parameter: str
    role: str
    purpose: str
    stored_path: str
    resolved_path: Optional[str]
    status: str
    required: bool = True
    candidates: Tuple[str, ...] = ()

    @property
    def is_missing(self) -> bool:
        """Whether the reference needs user attention before it can load."""
        return self.status in (STATUS_MISSING_PATH, STATUS_MISSING)

    @property
    def is_relink_candidate(self) -> bool:
        return self.status == STATUS_RELINK_CANDIDATE

    def to_dict(self) -> Dict[str, Any]:
        """Return a stable JSON-compatible representation for UI/logging."""
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "parameter": self.parameter,
            "role": self.role,
            "purpose": self.purpose,
            "stored_path": self.stored_path,
            "resolved_path": self.resolved_path,
            "status": self.status,
            "required": self.required,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class AssetDependencyReport:
    """Stable, read-only dependency report for a serialized composite graph."""

    references: Tuple[AssetReference, ...] = ()

    @property
    def missing(self) -> Tuple[AssetReference, ...]:
        return tuple(reference for reference in self.references
                     if reference.is_missing)

    @property
    def relink_candidates(self) -> Tuple[AssetReference, ...]:
        return tuple(reference for reference in self.references
                     if reference.is_relink_candidate)

    @property
    def resolved(self) -> Tuple[AssetReference, ...]:
        return tuple(reference for reference in self.references
                     if reference.status == STATUS_RESOLVED)

    def by_role(self, role: str) -> Tuple[AssetReference, ...]:
        return tuple(reference for reference in self.references
                     if reference.role == role)

    def to_dict(self) -> Dict[str, Any]:
        """Return a deterministic report suitable for persistence or export."""
        return {
            "references": [reference.to_dict() for reference in self.references],
            "counts": {
                "total": len(self.references),
                "missing": len(self.missing),
                "relink_candidates": len(self.relink_candidates),
                "resolved": len(self.resolved),
                "unverified": sum(
                    reference.status == STATUS_UNVERIFIED
                    for reference in self.references
                ),
            },
        }

    def format_summary(self) -> str:
        """Build a concise human-readable offline/relink summary."""
        if not self.references:
            return "未发现显式素材依赖。"
        lines = [
            f"素材依赖 {len(self.references)} 项："
            f"缺失 {len(self.missing)}，"
            f"重链接候选 {len(self.relink_candidates)}，"
            f"已解析 {len(self.resolved)}。"
        ]
        for reference in self.missing:
            path = reference.stored_path or "<未设置>"
            lines.append(
                f"  [missing] {reference.node_id}"
                f".{reference.parameter}: {path}"
            )
        for reference in self.relink_candidates:
            lines.append(
                f"  [relink] {reference.node_id}"
                f".{reference.parameter}: {reference.resolved_path}"
            )
        return "\n".join(lines)


class AssetRelinkError(ValueError):
    """A requested relink is stale, ambiguous, or outside the report."""


class AssetCollectError(ValueError):
    """A dependency cannot be collected safely into the requested directory."""


@dataclass(frozen=True)
class AssetRelinkChange:
    """One exact old-path → approved candidate-path mutation."""

    node_id: str
    parameter: str
    old_path: str
    new_path: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "node_id": self.node_id,
            "parameter": self.parameter,
            "old_path": self.old_path,
            "new_path": self.new_path,
        }


@dataclass(frozen=True)
class AssetRelinkResult:
    """Result of one atomic batch relink."""

    changes: Tuple[AssetRelinkChange, ...] = ()

    @property
    def changed_count(self) -> int:
        return len(self.changes)


@dataclass(frozen=True)
class AssetCollectCopy:
    """One source file and its deterministic destination in a collection."""

    source_path: str
    target_path: str
    digest: str
    size: int
    references: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_path": self.source_path,
            "target_path": self.target_path,
            "digest": self.digest,
            "size": self.size,
            "references": [list(reference) for reference in self.references],
        }


@dataclass(frozen=True)
class AssetCollectPlan:
    """Immutable copy and graph-update plan produced before any writes."""

    target_dir: str
    project_dir: Optional[str]
    store_relative_to_project: bool
    copies: Tuple[AssetCollectCopy, ...] = ()
    changes: Tuple[AssetRelinkChange, ...] = ()

    @property
    def changed_count(self) -> int:
        return len(self.changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_dir": self.target_dir,
            "project_dir": self.project_dir,
            "store_relative_to_project": self.store_relative_to_project,
            "copies": [item.to_dict() for item in self.copies],
            "changes": [item.to_dict() for item in self.changes],
        }


@dataclass(frozen=True)
class AssetCollectResult:
    """Result of applying one asset collection plan."""

    plan: AssetCollectPlan
    copied_count: int = 0
    reused_count: int = 0

    @property
    def changed_count(self) -> int:
        return self.plan.changed_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "copied_count": self.copied_count,
            "reused_count": self.reused_count,
            "changed_count": self.changed_count,
            "plan": self.plan.to_dict(),
        }


class AssetCollectService:
    """Collect resolved graph dependencies into a portable asset directory.

    Planning is read-only. Applying a plan copies files into the explicit
    target directory and updates the graph only after every source has been
    verified. Existing destination files are reused only when their SHA-256
    matches the planned source, so a collection can never silently overwrite
    unrelated data.
    """

    def __init__(self, graph: Any, undo_stack: Any = None) -> None:
        self.graph = graph
        self.undo_stack = undo_stack

    def plan(
        self,
        target_dir: str,
        *,
        project_dir: Optional[str] = None,
        asset_search_roots: Iterable[str] = (),
        store_relative_to_project: bool = True,
    ) -> AssetCollectPlan:
        """Build a deterministic collection plan without mutating anything."""
        target_root = _absolute_directory(target_dir)
        project_root = (
            os.path.abspath(os.path.expanduser(os.fspath(project_dir)))
            if project_dir else None
        )
        report = collect_asset_dependencies(
            self.graph.to_dict(), check_fs=True,
            project_dir=project_root, asset_search_roots=asset_search_roots)
        missing = tuple(reference for reference in report.references
                        if reference.is_missing)
        if missing:
            names = ", ".join(
                f"{item.node_id}.{item.parameter}" for item in missing)
            raise AssetCollectError(
                f"cannot collect missing asset dependencies: {names}")

        grouped: Dict[Tuple[str, str, str], List[AssetReference]] = {}
        for reference in report.references:
            if not reference.resolved_path:
                continue
            source_path = os.path.abspath(reference.resolved_path)
            digest, size = _file_digest(source_path)
            basename = os.path.basename(source_path) or "asset"
            grouped.setdefault((reference.role, basename, digest), []).append(
                reference)

        copies: List[AssetCollectCopy] = []
        changes: List[AssetRelinkChange] = []
        occupied: Dict[str, str] = {}
        for group_key in sorted(grouped):
            role, basename, digest = group_key
            references = tuple(sorted(
                grouped[group_key],
                key=lambda item: (item.node_id, item.parameter),
            ))
            relative_name = os.path.join(role, basename)
            destination = os.path.abspath(
                os.path.join(target_root, relative_name))
            prior_digest = occupied.get(destination)
            if prior_digest is not None and prior_digest != digest:
                relative_name = os.path.join(
                    role, f"{digest[:12]}_{basename}")
                destination = os.path.abspath(
                    os.path.join(target_root, relative_name))
            while (destination in occupied and occupied[destination] != digest):
                stem, extension = os.path.splitext(basename)
                relative_name = os.path.join(
                    role, f"{digest[:16]}_{stem}{extension}")
                destination = os.path.abspath(
                    os.path.join(target_root, relative_name))
            occupied[destination] = digest
            copy_item = AssetCollectCopy(
                source_path=os.path.abspath(references[0].resolved_path or ""),
                target_path=destination, digest=digest, size=size,
                references=tuple((item.node_id, item.parameter)
                                 for item in references),
            )
            copies.append(copy_item)
            stored_path = (
                _project_relative_path(destination, project_root)
                if store_relative_to_project else destination
            )
            for reference in references:
                changes.append(AssetRelinkChange(
                    node_id=reference.node_id,
                    parameter=reference.parameter,
                    old_path=reference.stored_path,
                    new_path=stored_path,
                ))

        return AssetCollectPlan(
            target_dir=target_root,
            project_dir=project_root,
            store_relative_to_project=bool(store_relative_to_project),
            copies=tuple(copies), changes=tuple(changes))

    def apply(self, plan: AssetCollectPlan) -> AssetCollectResult:
        """Copy files and update the graph atomically with stale checks."""
        if not isinstance(plan, AssetCollectPlan):
            raise AssetCollectError("plan must be an AssetCollectPlan")
        relink = AssetRelinkService(self.graph)
        for change in plan.changes:
            node = self.graph.get_node(change.node_id)
            if node is None or _read_live_reference(node, change.parameter) != change.old_path:
                raise AssetCollectError(
                    f"stale asset reference: {change.node_id}.{change.parameter}")

        copied_count = 0
        reused_count = 0
        for item in plan.copies:
            if not os.path.isfile(item.source_path):
                raise AssetCollectError(
                    f"source disappeared: {item.source_path}")
            source_digest, source_size = _file_digest(item.source_path)
            if source_digest != item.digest or source_size != item.size:
                raise AssetCollectError(
                    f"source changed since planning: {item.source_path}")
            if os.path.isfile(item.target_path):
                target_digest, target_size = _file_digest(item.target_path)
                if target_digest != item.digest or target_size != item.size:
                    raise AssetCollectError(
                        f"destination contains different data: {item.target_path}")
                reused_count += 1
                continue
            os.makedirs(os.path.dirname(item.target_path), exist_ok=True)
            shutil.copy2(item.source_path, item.target_path)
            copied_count += 1

        applied: List[AssetRelinkChange] = []
        try:
            for change in plan.changes:
                if not relink._set_live_reference(change, change.new_path):
                    raise AssetCollectError(
                        f"unable to update asset reference: {change.node_id}."
                        f"{change.parameter}")
                applied.append(change)
        except Exception:
            for change in reversed(applied):
                relink._set_live_reference(change, change.old_path)
            raise

        if plan.changes and self.undo_stack is not None:
            from .undo_stack import UndoAction
            data = {"changes": [change.to_dict() for change in plan.changes]}
            self.undo_stack.push(UndoAction(
                "asset_collect", data,
                undo_fn=lambda payload: relink._apply_history(payload, "old_path"),
                redo_fn=lambda payload: relink._apply_history(payload, "new_path"),
                description=f"Collect {len(plan.changes)} asset(s)",
            ))
        return AssetCollectResult(
            plan=plan, copied_count=copied_count, reused_count=reused_count)


class AssetRelinkService:
    """Safely apply approved dependency candidates to a live NodeGraph.

    The service never searches for a replacement by basename on its own.  The
    caller supplies ``(node_id, parameter) -> candidate_path`` selections, and
    each candidate must exactly match the current read-only dependency report.
    When an ``UndoStack`` is supplied, the whole batch becomes one undo step.
    """

    def __init__(self, graph: Any, undo_stack: Any = None) -> None:
        self.graph = graph
        self.undo_stack = undo_stack

    def plan(
        self,
        replacements: Mapping[Tuple[str, str], Any],
        *,
        project_dir: Optional[str] = None,
        asset_search_roots: Iterable[str] = (),
        store_relative_to_project: bool = False,
    ) -> Tuple[AssetRelinkChange, ...]:
        """Validate explicit candidate selections and return an immutable plan."""
        if not isinstance(replacements, Mapping):
            raise AssetRelinkError("replacements must be a mapping")

        report = collect_asset_dependencies(
            self.graph.to_dict(), check_fs=True,
            project_dir=project_dir, asset_search_roots=asset_search_roots)
        references = {
            (reference.node_id, reference.parameter): reference
            for reference in report.references
        }
        changes: List[AssetRelinkChange] = []
        for raw_key, raw_candidate in sorted(
                replacements.items(), key=lambda item: str(item[0])):
            if (not isinstance(raw_key, (tuple, list))
                    or len(raw_key) != 2):
                raise AssetRelinkError(
                    "relink key must be (node_id, parameter)")
            key = (str(raw_key[0]), str(raw_key[1]))
            reference = references.get(key)
            if reference is None:
                raise AssetRelinkError(
                    f"asset reference not found: {key[0]}.{key[1]}")
            if not reference.is_relink_candidate or not reference.resolved_path:
                raise AssetRelinkError(
                    f"asset reference is not an approved relink candidate: "
                    f"{key[0]}.{key[1]}")

            candidate = _path_text(raw_candidate)
            candidate_abs = os.path.abspath(os.path.expanduser(candidate))
            expected_abs = os.path.abspath(reference.resolved_path)
            if candidate_abs != expected_abs:
                raise AssetRelinkError(
                    f"candidate does not match report for {key[0]}.{key[1]}")
            node = self.graph.get_node(reference.node_id)
            if node is None:
                raise AssetRelinkError(
                    f"node no longer exists: {reference.node_id!r}")
            current_path = _read_live_reference(node, reference.parameter)
            if current_path != reference.stored_path:
                raise AssetRelinkError(
                    f"stale asset reference: {reference.node_id}."
                    f"{reference.parameter}")
            changes.append(AssetRelinkChange(
                node_id=reference.node_id, parameter=reference.parameter,
                old_path=reference.stored_path,
                new_path=(
                    _project_relative_path(expected_abs, project_dir)
                    if store_relative_to_project else expected_abs
                )))
        return tuple(changes)

    def apply(
        self,
        replacements: Mapping[Tuple[str, str], Any],
        *,
        project_dir: Optional[str] = None,
        asset_search_roots: Iterable[str] = (),
        store_relative_to_project: bool = False,
    ) -> AssetRelinkResult:
        """Apply a validated batch atomically and optionally record one undo step."""
        changes = self.plan(
            replacements, project_dir=project_dir,
            asset_search_roots=asset_search_roots,
            store_relative_to_project=store_relative_to_project)
        applied: List[AssetRelinkChange] = []
        try:
            for change in changes:
                if not self._set_live_reference(change, change.new_path):
                    raise AssetRelinkError(
                        f"unable to update asset reference: {change.node_id}."
                        f"{change.parameter}")
                applied.append(change)
        except Exception:
            for change in reversed(applied):
                self._set_live_reference(change, change.old_path)
            raise

        if changes and self.undo_stack is not None:
            from .undo_stack import UndoAction
            data = {"changes": [change.to_dict() for change in changes]}
            self.undo_stack.push(UndoAction(
                "asset_relink", data,
                undo_fn=lambda payload: self._apply_history(payload, "old_path"),
                redo_fn=lambda payload: self._apply_history(payload, "new_path"),
                description=f"Relink {len(changes)} asset(s)",
            ))
        return AssetRelinkResult(changes=changes)

    def _apply_history(self, data: Mapping[str, Any], path_key: str) -> None:
        changes = data.get("changes") or ()
        for item in changes:
            if not isinstance(item, Mapping):
                continue
            change = AssetRelinkChange(
                node_id=str(item.get("node_id", "")),
                parameter=str(item.get("parameter", "")),
                old_path=str(item.get("old_path", "")),
                new_path=str(item.get("new_path", "")),
            )
            path = change.old_path if path_key == "old_path" else change.new_path
            if not self._set_live_reference(change, path):
                raise AssetRelinkError(
                    f"unable to restore asset reference: {change.node_id}."
                    f"{change.parameter}")

    def _set_live_reference(self, change: AssetRelinkChange, path: str) -> bool:
        node = self.graph.get_node(change.node_id)
        if node is None:
            return False
        material_location = _material_override_location(change.parameter)
        if material_location is None:
            return bool(self.graph.set_node_parameter(
                change.node_id, change.parameter, path))

        material_id, slot_name = material_location
        raw = node.parameters.get("material_overrides", "")
        decoded = _decode_override_payload(raw)
        if decoded is None:
            return False
        if isinstance(decoded, list):
            item = _override_list_item(decoded, material_id)
        else:
            item = decoded.get(material_id)
        if not isinstance(item, dict):
            return False
        slots = item.get("slot_overrides")
        if not isinstance(slots, dict) or not isinstance(slots.get(slot_name), dict):
            return False
        slots[slot_name]["path"] = path
        encoded = json.dumps(decoded, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        return bool(self.graph.set_node_parameter(
            change.node_id, "material_overrides", encoded))


def collect_asset_dependencies(
    graph_data: Optional[Mapping[str, Any]],
    *,
    check_fs: bool = True,
    project_dir: Optional[str] = None,
    asset_search_roots: Iterable[str] = (),
) -> AssetDependencyReport:
    """Collect explicit asset references from serialized composite data.

    No graph or filesystem mutation occurs.  When ``check_fs`` is false, paths
    are marked ``unverified`` so callers can still display the dependency list
    without pretending that a path was checked.
    """
    if not isinstance(graph_data, Mapping):
        return AssetDependencyReport()

    references: List[AssetReference] = []
    nodes = graph_data.get("nodes") or ()
    if not isinstance(nodes, (list, tuple)):
        return AssetDependencyReport()
    search_roots = tuple(asset_search_roots)

    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_id = str(node.get("id", "") or "")
        node_type = str(node.get("type", "") or "")
        if not node_id:
            continue
        # AUD-4707: a disabled node is bypassed at render time, so its
        # assets (including empty required paths) are not collectable
        # dependencies and must not block the whole collection.
        if bool(node.get("disabled", False)):
            continue
        parameters = node.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            parameters = {}

        for parameter, role, purpose, raw_path, required in _iter_node_paths(
                node_type, parameters):
            references.append(_make_reference(
                node_id=node_id,
                node_type=node_type,
                parameter=parameter,
                role=role,
                purpose=purpose,
                raw_path=raw_path,
                required=required,
                check_fs=check_fs,
                project_dir=project_dir,
                asset_search_roots=search_roots,
            ))

    references.sort(key=lambda item: (
        item.node_id, item.parameter, item.role, item.stored_path))
    return AssetDependencyReport(references=tuple(references))


def _iter_node_paths(
    node_type: str,
    parameters: Mapping[str, Any],
) -> Iterable[Tuple[str, str, str, Any, bool]]:
    """Yield ``parameter, role, purpose, path, required`` tuples."""
    if node_type in {"video_frame", "image_input"}:
        yield ("file_path", ROLE_MEDIA, "媒体源", parameters.get("file_path", ""), True)

    if node_type == "model3d" and bool(parameters.get("use_imported_model", False)):
        yield ("model_path", ROLE_MODEL, "3D 模型", parameters.get("model_path", ""), True)
        yield from _iter_material_override_paths(parameters.get("material_overrides"))

    if node_type == "lut":
        path = parameters.get("lut_path", "")
        if _has_text(path):
            yield ("lut_path", ROLE_LUT, "3D LUT", path, True)

    if node_type == "light3d":
        hdri_path = parameters.get("hdri_path", "")
        if bool(parameters.get("use_ibl", True)) and _has_text(hdri_path):
            yield ("hdri_path", ROLE_HDRI, "HDRI 环境贴图", hdri_path, True)
        ies_path = parameters.get("ies_profile", "")
        if bool(parameters.get("ies_enabled", False)):
            # Keep an empty enabled slot in the report: the render path
            # currently disables it silently, which is precisely the kind of
            # hidden dependency this report is intended to expose.
            yield ("ies_profile", ROLE_IES, "IES 光度学配置", ies_path, True)

    if node_type == "scene3d":
        # Scene3D accepts built-in ``__sky__``/``__none__`` values and simple
        # preset names.  Only explicit file-like values are dependencies.
        hdri_path = parameters.get("hdri_preset", "")
        if (bool(parameters.get("use_ibl", True))
                and _looks_like_external_path(hdri_path)):
            yield ("hdri_preset", ROLE_HDRI, "HDRI 环境贴图", hdri_path, True)


def _iter_material_override_paths(
    raw_overrides: Any,
) -> Iterable[Tuple[str, str, str, Any, bool]]:
    """Extract explicit texture-slot overrides without evaluating materials."""
    if raw_overrides in (None, ""):
        return
    raw: Any = raw_overrides
    if isinstance(raw_overrides, str):
        try:
            raw = json.loads(raw_overrides)
        except (TypeError, ValueError, json.JSONDecodeError):
            return

    if isinstance(raw, list):
        items = ((str(item.get("target_material_id", index)), item)
                 for index, item in enumerate(raw)
                 if isinstance(item, Mapping))
    elif isinstance(raw, Mapping):
        items = ((str(key), item) for key, item in raw.items())
    else:
        return

    for material_id, item in items:
        if not isinstance(item, Mapping):
            continue
        slots = item.get("slot_overrides") or {}
        if not isinstance(slots, Mapping):
            continue
        for slot_name, slot in sorted(slots.items(), key=lambda pair: str(pair[0])):
            if not isinstance(slot, Mapping):
                continue
            path = slot.get("path", "")
            if not _has_text(path):
                continue
            parameter = (
                f"material_overrides[{material_id}]"
                f".slot_overrides[{slot_name}].path"
            )
            yield (parameter, ROLE_MATERIAL_TEXTURE,
                   f"材质覆写贴图（{material_id}/{slot_name}）", path, True)


def _material_override_location(parameter: str) -> Optional[Tuple[str, str]]:
    """Decode the synthetic report parameter for a material slot path."""
    prefix = "material_overrides["
    marker = "].slot_overrides["
    suffix = "].path"
    if not parameter.startswith(prefix) or marker not in parameter:
        return None
    material_id, remainder = parameter[len(prefix):].split(marker, 1)
    if not remainder.endswith(suffix):
        return None
    slot_name = remainder[:-len(suffix)]
    if not material_id or not slot_name:
        return None
    return material_id, slot_name


def _decode_override_payload(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if isinstance(raw, (dict, list)):
        return raw
    return None


def _override_list_item(items: List[Any], material_id: str) -> Any:
    try:
        index = int(material_id)
    except (TypeError, ValueError):
        index = -1
    if 0 <= index < len(items) and isinstance(items[index], dict):
        item = items[index]
        if str(item.get("target_material_id", index)) == material_id:
            return item
    for item in items:
        if (isinstance(item, dict)
                and str(item.get("target_material_id", "")) == material_id):
            return item
    return None


def _read_live_reference(node: Any, parameter: str) -> str:
    location = _material_override_location(parameter)
    if location is None:
        return _path_text(getattr(node, "parameters", {}).get(parameter, ""))
    material_id, slot_name = location
    decoded = _decode_override_payload(
        getattr(node, "parameters", {}).get("material_overrides", ""))
    if isinstance(decoded, list):
        item = _override_list_item(decoded, material_id)
    elif isinstance(decoded, dict):
        item = decoded.get(material_id)
    else:
        item = None
    if not isinstance(item, dict):
        return ""
    slots = item.get("slot_overrides")
    if not isinstance(slots, dict) or not isinstance(slots.get(slot_name), dict):
        return ""
    return _path_text(slots[slot_name].get("path", ""))


def _make_reference(
    *,
    node_id: str,
    node_type: str,
    parameter: str,
    role: str,
    purpose: str,
    raw_path: Any,
    required: bool,
    check_fs: bool,
    project_dir: Optional[str],
    asset_search_roots: Iterable[str],
) -> AssetReference:
    stored_path = _path_text(raw_path)
    if not stored_path:
        return AssetReference(
            node_id=node_id, node_type=node_type, parameter=parameter,
            role=role, purpose=purpose, stored_path="", resolved_path=None,
            status=STATUS_MISSING_PATH, required=required)
    if not check_fs:
        return AssetReference(
            node_id=node_id, node_type=node_type, parameter=parameter,
            role=role, purpose=purpose, stored_path=stored_path,
            resolved_path=None, status=STATUS_UNVERIFIED, required=required)

    resolution = resolve_asset_path(
        stored_path, project_dir=project_dir,
        search_roots=asset_search_roots)
    if not resolution.is_resolved:
        return AssetReference(
            node_id=node_id, node_type=node_type, parameter=parameter,
            role=role, purpose=purpose, stored_path=stored_path,
            resolved_path=None, status=STATUS_MISSING, required=required,
            candidates=resolution.candidates)

    resolved_path = resolution.resolved_path
    original_path = os.path.abspath(os.path.expanduser(stored_path))
    status = (STATUS_RESOLVED if resolved_path == original_path
              else STATUS_RELINK_CANDIDATE)
    return AssetReference(
        node_id=node_id, node_type=node_type, parameter=parameter,
        role=role, purpose=purpose, stored_path=stored_path,
        resolved_path=resolved_path, status=status, required=required,
        candidates=resolution.candidates)


def _path_text(value: Any) -> str:
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return value.strip() if isinstance(value, str) else ""


def _absolute_directory(value: Any) -> str:
    path = _path_text(value)
    if not path:
        raise AssetCollectError("target_dir must not be empty")
    return os.path.abspath(os.path.expanduser(path))


def _file_digest(path: str) -> Tuple[str, int]:
    """Return a streaming SHA-256 and byte size for a regular file."""
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except (OSError, TypeError) as exc:
        raise AssetCollectError(f"unable to read asset: {path}") from exc
    return digest.hexdigest(), size


def _has_text(value: Any) -> bool:
    return bool(_path_text(value))


def _looks_like_external_path(value: Any) -> bool:
    path = _path_text(value)
    if not path or path in {"__sky__", "__none__"}:
        return False
    extension = os.path.splitext(path)[1].lower()
    return os.path.isabs(path) or os.sep in path or extension in {
        ".hdr", ".exr", ".hdri", ".jpg", ".jpeg", ".png", ".tif", ".tiff"
    }


def _project_relative_path(path: str, project_dir: Optional[str]) -> str:
    """Return a portable project-relative path when ``path`` is inside root."""
    if not project_dir:
        return path
    root = os.path.abspath(os.path.expanduser(os.fspath(project_dir)))
    absolute = os.path.abspath(os.path.expanduser(path))
    try:
        if os.path.commonpath((root, absolute)) != root:
            return path
    except ValueError:
        # Different drives on Windows cannot share a relative path.  Keeping
        # the absolute candidate is safer than writing an unusable path.
        return path
    return os.path.relpath(absolute, root)
