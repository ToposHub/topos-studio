"""Asset / media-pool / bin commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: asset cluster (project bins, assets,
media import / replace / relink / proxy, smart bins, best-take marks, and the
timeline-settings project transaction). Cluster-private helpers
(_record_field / _detach_asset_from_composition / _registered_scene_id_for_asset /
_unregister_duplicated_scene) moved with the cluster; shared symbols
(assert_track_editable, ProjectTransactionResult) live in _support.py.
Class sources are byte-identical to the original module; to_dict/from_dict
keys are a persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
import copy
import time
import uuid
import logging

from ._support import ProjectTransactionResult, assert_track_editable

logger = logging.getLogger(__name__)

# ================================================================
# Bin Commands - Project-level Bin Operations
# ================================================================

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    SmartBin,
    TimelineSequenceRef,
    relink_asset,
    batch_relink_assets,
    replace_media,
    update_asset_proxy,
)


@dataclass(frozen=True)
class CreateBinCommand:
    """
    Command to create a new bin in the project.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        name: Bin name
        parent_id: Parent bin ID (None for root level)
        _created_bin_id: Internal: ID of created bin (set during apply)
    """

    command_id: str
    label: str
    name: str
    parent_id: str | None = None
    _created_bin_id: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: create bin.

        Args:
            project: Current project

        Returns:
            New project with bin created
        """
        new_project = project.create_bin(self.name, self.parent_id)

        # Find the newly created bin ID and persist it on this frozen command
        # (via the object.__setattr__ escape hatch the rest of this module
        # already uses) so callers can look it up after apply() — e.g. to drop
        # the new bin into inline-edit mode right after creation — and so undo
        # reliably removes exactly this bin instead of guessing by name.
        new_bin_id = None
        for bin_id in new_project.bins.keys():
            if bin_id not in project.bins:
                new_bin_id = bin_id
                break
        if new_bin_id is not None:
            object.__setattr__(self, "_created_bin_id", new_bin_id)

        logger.debug(f"CreateBinCommand applied: {self.name} (id={new_bin_id})")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: delete the bin.

        Args:
            project: Current project

        Returns:
            Project without the bin
        """
        if self._created_bin_id is None:
            # Try to find the bin by name that wasn't in original project
            # This is a fallback for undo when we don't have the ID stored
            for bin_id, bin_obj in project.bins.items():
                if bin_obj.name == self.name and bin_id != project.root_bin_id:
                    return project.remove_bin(bin_id)
            logger.warning("CreateBinCommand undo: cannot find created bin")
            return project

        new_project = project.remove_bin(self._created_bin_id)
        logger.debug(f"CreateBinCommand undone: removed bin {self._created_bin_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "CreateBinCommand",
            "label": self.label,
            "name": self.name,
            "parent_id": self.parent_id,
            "created_bin_id": self._created_bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CreateBinCommand:
        """Deserialize command."""
        return CreateBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Create Bin"),
            name=data.get("name", "New Bin"),
            parent_id=data.get("parent_id"),
            _created_bin_id=data.get("created_bin_id"),
        )

    @staticmethod
    def create(name: str, parent_id: str | None = None) -> CreateBinCommand:
        """Factory method to create command."""
        return CreateBinCommand(
            command_id=str(uuid.uuid4()),
            label=f"Create Bin '{name}'",
            name=name,
            parent_id=parent_id,
        )


@dataclass(frozen=True)
class DeleteBinCommand:
    """
    Command to delete a bin from the project.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        bin_id: Bin ID to delete
        _deleted_bin: Internal: deleted bin data for undo
        _deleted_asset_ids: Internal: asset IDs that were in the bin
        _deleted_descendants: Internal: AUD-16213——被删 bin 的全部后代 bin
            快照（递归，含各自 parent_id/asset_ids/timeline_ids），undo 时
            一并还原缺失或被改挂的子 bin，覆盖 remove_bin 未来可能的
            reparent/级联语义变化。
    """

    command_id: str
    label: str
    bin_id: str
    _deleted_bin: dict[str, Any] | None = None  # Set during apply
    _deleted_asset_ids: tuple[str, ...] = ()  # Set during apply
    _deleted_descendants: tuple[dict[str, Any], ...] = ()  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: delete bin.

        Args:
            project: Current project

        Returns:
            New project without the bin
        """
        if self.bin_id == project.root_bin_id:
            raise ValueError("Cannot delete root bin")

        bin_obj = project.bins.get(self.bin_id)
        if bin_obj is None:
            logger.warning(f"DeleteBinCommand: bin {self.bin_id} not found")
            return project

        # Store bin data for undo. The dataclass is frozen, so use the
        # object.__setattr__ escape hatch that the rest of this module already
        # uses for the same purpose.
        deleted_bin_data = {
            "bin_id": bin_obj.bin_id,
            "name": bin_obj.name,
            "parent_id": bin_obj.parent_id,
            "asset_ids": bin_obj.asset_ids,
            "timeline_ids": bin_obj.timeline_ids,
        }
        object.__setattr__(self, "_deleted_bin", deleted_bin_data)
        object.__setattr__(self, "_deleted_asset_ids", bin_obj.asset_ids)

        # AUD-16213: 快照全部后代 bin（递归）。当前 EditProject.remove_bin
        # 只删除 bin 记录本身（子 bin 悬挂在其 parent_id 上，undo 恢复
        # 记录后引用重新生效），此快照是幂等 no-op；若 remove_bin 将来
        # 改为 reparent 到根或级联删除，undo 仍能完整还原父子关系与
        # 子 bin 记录，而不是只恢复顶层 bin。
        children_of: dict[str | None, list[Any]] = {}
        for other in project.bins.values():
            children_of.setdefault(other.parent_id, []).append(other)
        descendants: list[dict[str, Any]] = []
        pending = list(children_of.get(self.bin_id, ()))
        while pending:
            descendant = pending.pop()
            descendants.append({
                "bin_id": descendant.bin_id,
                "name": descendant.name,
                "parent_id": descendant.parent_id,
                "asset_ids": descendant.asset_ids,
                "timeline_ids": descendant.timeline_ids,
            })
            pending.extend(children_of.get(descendant.bin_id, ()))
        object.__setattr__(self, "_deleted_descendants", tuple(descendants))

        new_project = project.remove_bin(self.bin_id)
        logger.debug(f"DeleteBinCommand applied: deleted bin {self.bin_id}")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: restore the deleted bin.

        Args:
            project: Current project

        Returns:
            Project with bin restored
        """
        if self._deleted_bin is None:
            logger.warning("DeleteBinCommand undo: no deleted bin data")
            return project

        # Re-create the bin
        from src.features.edit.core.project import ProjectBin

        restored_bin = ProjectBin(
            bin_id=self._deleted_bin["bin_id"],
            name=self._deleted_bin["name"],
            parent_id=self._deleted_bin["parent_id"],
            asset_ids=self._deleted_bin["asset_ids"],
            timeline_ids=self._deleted_bin["timeline_ids"],
        )

        # Add bin back to project (use replace to preserve all other fields)
        new_bins = dict(project.bins)
        new_bins[restored_bin.bin_id] = restored_bin

        # AUD-16213: 还原后代 bin——缺失的整条记录重建，仍在但 parent_id
        # 被改挂（如 reparent 到 root）的恢复原父子关系。当前 remove_bin
        # 语义下这两类情况都不会发生，此分支是防御性等值恢复。
        for descendant_data in self._deleted_descendants:
            existing = new_bins.get(descendant_data["bin_id"])
            if existing is not None and existing.parent_id == descendant_data["parent_id"]:
                continue
            new_bins[descendant_data["bin_id"]] = ProjectBin(
                bin_id=descendant_data["bin_id"],
                name=descendant_data["name"],
                parent_id=descendant_data["parent_id"],
                asset_ids=descendant_data["asset_ids"],
                timeline_ids=descendant_data["timeline_ids"],
            )

        import time as time_module
        new_project = replace(
            project,
            bins=new_bins,
            modified_at=time_module.time(),
        )

        logger.debug(f"DeleteBinCommand undone: restored bin {self.bin_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "DeleteBinCommand",
            "label": self.label,
            "bin_id": self.bin_id,
            "deleted_bin": self._deleted_bin,
            "deleted_asset_ids": list(self._deleted_asset_ids),
            "deleted_descendants": [
                {
                    "bin_id": d["bin_id"],
                    "name": d["name"],
                    "parent_id": d["parent_id"],
                    "asset_ids": list(d["asset_ids"]),
                    "timeline_ids": list(d["timeline_ids"]),
                }
                for d in self._deleted_descendants
            ],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteBinCommand:
        """Deserialize command."""
        deleted_asset_ids = data.get("deleted_asset_ids") or []
        deleted_bin = data.get("deleted_bin")
        descendants = []
        for descendant in (data.get("deleted_descendants") or []):
            try:
                descendants.append({
                    "bin_id": descendant["bin_id"],
                    "name": descendant["name"],
                    "parent_id": descendant.get("parent_id"),
                    "asset_ids": tuple(descendant.get("asset_ids") or ()),
                    "timeline_ids": tuple(descendant.get("timeline_ids") or ()),
                })
            except (KeyError, TypeError, AttributeError) as exc:
                logger.warning(
                    "DeleteBinCommand.from_dict: skipping malformed "
                    "descendant snapshot %r (%s)", descendant, exc
                )
        return DeleteBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Bin"),
            bin_id=data.get("bin_id", ""),
            _deleted_bin=deleted_bin,
            _deleted_asset_ids=tuple(deleted_asset_ids),
            _deleted_descendants=tuple(descendants),
        )

    @staticmethod
    def create(bin_id: str) -> DeleteBinCommand:
        """Factory method to create command."""
        return DeleteBinCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Bin",
            bin_id=bin_id,
        )


def _record_field(record: Any, field_name: str, default: Any = None) -> Any:
    """Read a field from either a persisted dict or a typed record."""
    if isinstance(record, dict):
        return record.get(field_name, default)
    return getattr(record, field_name, default)


def _detach_asset_from_composition(
    composition: Any,
    asset_id: str,
) -> tuple[Any, bool]:
    """Detach one deleted media asset from a persisted composite graph.

    Composite graphs intentionally keep their source file path so removing an
    item from the Media Pool never removes the user's original file.  The
    asset id is only a project-local lookup key, however, so it must not stay
    in the graph after the asset has been removed.  Timeline input nodes also
    need their timeline/clip ids cleared; otherwise the preview provider can
    continue resolving a deleted timeline reference.

    ``source_kind=detached_asset`` makes the state explicit and lets the graph
    serializer load a valid composition even though its original project
    asset is no longer present.
    """
    if not isinstance(composition, dict):
        return composition, False

    detached = copy.deepcopy(composition)
    changed = False

    def clear_field(mapping: dict[str, Any], key: str) -> None:
        nonlocal changed
        if mapping.get(key) == asset_id:
            mapping[key] = ""
            changed = True

    # Project-level source metadata is not used to decode the file, but it is
    # still a persisted asset reference and must not point at a deleted id.
    for key in ("source_asset_id", "comp_asset_id", "original_asset_id"):
        clear_field(detached, key)

    if detached.get("source_asset_id") == "":
        # A source clip without its media-pool asset is no longer a live
        # timeline-backed composition.  Keep source_asset_path for direct
        # file-based nodes, but detach the project identity.
        if detached.get("source_kind") != "empty_comp_clip":
            if detached.get("source_kind") != "detached_asset":
                detached["source_kind"] = "detached_asset"
                changed = True
            for key in ("source_clip_id", "clip_id", "timeline_id"):
                if detached.get(key):
                    detached[key] = ""
                    changed = True

    source_refs = detached.get("source_refs")
    if isinstance(source_refs, dict):
        for key in ("original_asset_id", "asset_id"):
            clear_field(source_refs, key)
        for key in ("original_clip_id", "clip_id", "timeline_id"):
            if source_refs.get(key) and detached.get("source_kind") == "detached_asset":
                source_refs[key] = ""
                changed = True

    nodes = detached.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            parameters = node.get("parameters")
            if not isinstance(parameters, dict):
                continue
            clear_field(parameters, "asset_id")
            if node.get("type") == "timeline_clip_input":
                # A timeline input cannot produce a frame once its source
                # clip/asset has been removed.  Make it an explicit detached
                # input instead of leaving a renderer-visible dangling ref.
                for key in ("timeline_id", "clip_id"):
                    if parameters.get(key):
                        parameters[key] = ""
                        changed = True

    if changed:
        detached["modified_at"] = time.time()
    return detached, changed


@dataclass(frozen=True)
class DeleteAssetCommand:
    """
    Command to delete an asset from the project.

    Captures the deleted ``MediaAsset`` snapshot plus the set of bins that
    referenced it during ``apply`` so that ``undo`` can fully restore both the
    asset and every bin's ``asset_ids`` tuple.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Asset ID to delete
        _deleted_asset: Internal: deleted MediaAsset snapshot (set during apply)
        _affected_bin_ids: Internal: bin IDs that referenced this asset
    """

    command_id: str
    label: str
    asset_id: str
    _deleted_asset: MediaAsset | None = None  # Set during apply
    _affected_bin_ids: tuple[str, ...] = ()  # Set during apply
    _deleted_comp_clips: tuple[tuple[str, Any], ...] = ()  # Set during apply
    _deleted_compositions: tuple[tuple[str, Any], ...] = ()  # Set during apply
    _modified_compositions: tuple[tuple[str, Any], ...] = ()  # Set during apply
    _previous_active_comp_clip_id: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: delete asset.

        Args:
            project: Current project

        Returns:
            New project without the asset
        """
        asset = project.assets.get(self.asset_id)
        if asset is None:
            logger.warning(f"DeleteAssetCommand: asset {self.asset_id} not found")
            return project

        # Snapshot every bin that currently references this asset, so undo
        # can restore their ``asset_ids`` tuples exactly.
        affected_bin_ids = tuple(
            bin_id for bin_id, bin_obj in project.bins.items()
            if self.asset_id in bin_obj.asset_ids
        )

        # Persist snapshots on the frozen dataclass via the same escape hatch
        # the rest of this module uses.
        object.__setattr__(self, "_deleted_asset", asset)
        object.__setattr__(self, "_affected_bin_ids", affected_bin_ids)

        # A Comp Clip is represented by three linked project records.  Remove
        # the complete unit instead of leaving its graph/metadata behind.
        comp_clip_ids: list[str] = []
        comp_clip_snapshots: list[tuple[str, Any]] = []
        deleted_composition_ids: set[str] = set()
        comp_clip_id_from_asset = (
            asset.metadata.get("comp_clip_id")
            if isinstance(asset.metadata, dict)
            else None
        )
        for comp_clip_id, comp_clip in project.comp_clips.items():
            if (
                _record_field(comp_clip, "asset_id") == self.asset_id
                or (
                    comp_clip_id_from_asset
                    and comp_clip_id == comp_clip_id_from_asset
                )
            ):
                comp_clip_ids.append(comp_clip_id)
                comp_clip_snapshots.append((comp_clip_id, comp_clip))
                graph_id = _record_field(comp_clip, "graph_id")
                if graph_id and graph_id in project.compositions:
                    deleted_composition_ids.add(graph_id)

        deleted_composition_snapshots = tuple(
            (graph_id, project.compositions[graph_id])
            for graph_id in deleted_composition_ids
        )
        modified_composition_snapshots: list[tuple[str, Any]] = []
        sanitized_compositions = dict(project.compositions)
        for graph_id, composition in project.compositions.items():
            if graph_id in deleted_composition_ids:
                sanitized_compositions.pop(graph_id, None)
                continue
            detached, changed = _detach_asset_from_composition(
                composition, self.asset_id
            )
            if changed:
                modified_composition_snapshots.append((graph_id, composition))
                sanitized_compositions[graph_id] = detached

        object.__setattr__(self, "_deleted_comp_clips", tuple(comp_clip_snapshots))
        object.__setattr__(
            self, "_deleted_compositions", deleted_composition_snapshots
        )
        object.__setattr__(
            self,
            "_modified_compositions",
            tuple(modified_composition_snapshots),
        )
        object.__setattr__(
            self,
            "_previous_active_comp_clip_id",
            project.active_comp_clip_id,
        )

        new_project = project
        for comp_clip_id in comp_clip_ids:
            new_project = new_project.remove_comp_clip(comp_clip_id)
        if not comp_clip_ids:
            new_project = new_project.remove_asset(self.asset_id)
        new_project = replace(
            new_project,
            compositions=sanitized_compositions,
            modified_at=time.time(),
        )
        logger.debug(f"DeleteAssetCommand applied: deleted asset {self.asset_id}")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """Undo the command: restore the deleted asset and bin references."""
        if self._deleted_asset is None:
            logger.warning("DeleteAssetCommand undo: no deleted asset snapshot")
            return project

        new_assets = dict(project.assets)
        new_assets[self._deleted_asset.asset_id] = self._deleted_asset

        new_bins = dict(project.bins)
        for bin_id in self._affected_bin_ids:
            bin_obj = new_bins.get(bin_id)
            if bin_obj is None:
                continue
            # Only restore if missing (idempotent against other commands)
            if self.asset_id not in bin_obj.asset_ids:
                new_bins[bin_id] = bin_obj.with_asset_added(self.asset_id)

        new_compositions = dict(project.compositions)
        for graph_id, composition in self._modified_compositions:
            new_compositions[graph_id] = composition
        for graph_id, composition in self._deleted_compositions:
            new_compositions[graph_id] = composition

        new_comp_clips = dict(project.comp_clips)
        for comp_clip_id, comp_clip in self._deleted_comp_clips:
            new_comp_clips[comp_clip_id] = comp_clip

        new_project = replace(
            project,
            assets=new_assets,
            bins=new_bins,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            active_comp_clip_id=self._previous_active_comp_clip_id,
            modified_at=time.time(),
        )

        logger.debug(f"DeleteAssetCommand undone: restored asset {self.asset_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command.

        Note: the ``_deleted_asset`` snapshot is intentionally omitted from the
        serialized form. The project command stack is currently in-memory only;
        if it is ever persisted, the asset should be re-fetched from the
        project's serialization layer (``EditProjectSerializer._asset_to_dict``).
        """
        return {
            "command_id": self.command_id,
            "command_type": "DeleteAssetCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "affected_bin_ids": list(self._affected_bin_ids),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteAssetCommand:
        """Deserialize command.

        The ``_deleted_asset`` snapshot cannot be restored from a serialized
        form; ``undo`` will log a warning and no-op if the snapshot is missing.
        """
        return DeleteAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Asset"),
            asset_id=data.get("asset_id", ""),
            _deleted_asset=None,
            _affected_bin_ids=tuple(data.get("affected_bin_ids", [])),
        )

    @staticmethod
    def create(asset_id: str) -> DeleteAssetCommand:
        """Factory method to create command."""
        return DeleteAssetCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Asset",
            asset_id=asset_id,
        )


@dataclass(frozen=True)
class DeleteTimelineCommand:
    """
    Command to delete a timeline reference from the project.

    Captures the deleted ``TimelineSequenceRef`` snapshot plus the previous
    ``active_timeline_id`` so that ``undo`` can fully restore both.

    Note: this command operates only on ``EditProject.timelines`` (the ref
    dictionary). The matching full ``TimelineSequence`` object is managed by
    the edit controller through the ``timeline_command_snapshot_requested``
    signal so tracks/clips survive an undo/redo cycle.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        timeline_id: Timeline ID to delete
        _deleted_ref: Internal: deleted TimelineSequenceRef (set during apply)
        _previous_active_id: Internal: active_timeline_id before deletion
    """

    command_id: str
    label: str
    timeline_id: str
    _deleted_ref: TimelineSequenceRef | None = None  # Set during apply
    _previous_active_id: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: delete timeline reference.

        Args:
            project: Current project

        Returns:
            New project without the timeline reference
        """
        timeline_ref = project.timelines.get(self.timeline_id)
        if timeline_ref is None:
            logger.warning(f"DeleteTimelineCommand: timeline {self.timeline_id} not found")
            return project

        # Persist snapshots on the frozen dataclass via the same escape hatch
        # the rest of this module uses.
        object.__setattr__(self, "_deleted_ref", timeline_ref)
        object.__setattr__(self, "_previous_active_id", project.active_timeline_id)

        new_project = project.remove_timeline(self.timeline_id)
        logger.debug(f"DeleteTimelineCommand applied: deleted timeline {self.timeline_id}")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """Undo the command: restore the deleted timeline reference."""
        if self._deleted_ref is None:
            logger.warning("DeleteTimelineCommand undo: no deleted ref snapshot")
            return project

        new_timelines = dict(project.timelines)
        new_timelines[self._deleted_ref.timeline_id] = self._deleted_ref

        # Restore active_timeline_id only when the deleted timeline was active;
        # otherwise leave the current active alone.
        new_active_id = project.active_timeline_id
        if self._previous_active_id == self.timeline_id:
            new_active_id = self.timeline_id

        import time as time_module
        new_project = replace(
            project,
            timelines=new_timelines,
            active_timeline_id=new_active_id,
            modified_at=time_module.time(),
        )

        logger.debug(f"DeleteTimelineCommand undone: restored timeline {self.timeline_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command.

        Note: the ``_deleted_ref`` snapshot is intentionally omitted (same
        rationale as ``DeleteAssetCommand``). The project command stack is
        in-memory only at the time of writing.
        """
        return {
            "command_id": self.command_id,
            "command_type": "DeleteTimelineCommand",
            "label": self.label,
            "timeline_id": self.timeline_id,
            "previous_active_id": self._previous_active_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteTimelineCommand:
        """Deserialize command."""
        return DeleteTimelineCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Timeline"),
            timeline_id=data.get("timeline_id", ""),
            _deleted_ref=None,
            _previous_active_id=data.get("previous_active_id"),
        )

    @staticmethod
    def create(timeline_id: str) -> DeleteTimelineCommand:
        """Factory method to create command."""
        return DeleteTimelineCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Timeline",
            timeline_id=timeline_id,
        )


@dataclass(frozen=True)
class RenameBinCommand:
    """
    Command to rename a bin.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        bin_id: Bin ID to rename
        new_name: New bin name
        old_name: Old bin name (for undo, set during apply)
    """

    command_id: str
    label: str
    bin_id: str
    new_name: str
    old_name: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: rename bin.

        Args:
            project: Current project

        Returns:
            New project with renamed bin
        """
        bin_obj = project.bins.get(self.bin_id)
        if bin_obj is None:
            raise ValueError(f"Bin not found: {self.bin_id}")

        # Persist the old name on this frozen command (via the
        # object.__setattr__ escape hatch) so undo() can restore it. The
        # factory leaves old_name=None; without this, undo was a no-op.
        if self.old_name is None:
            object.__setattr__(self, "old_name", bin_obj.name)
        stored_old_name = self.old_name

        new_project = project.rename_bin(self.bin_id, self.new_name)
        logger.debug(f"RenameBinCommand applied: '{stored_old_name}' -> '{self.new_name}'")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: restore old name.

        Args:
            project: Current project

        Returns:
            Project with original bin name
        """
        if self.old_name is None:
            # Fallback: use new_name to find the bin, then restore
            # We don't have the old name stored, so we can't undo properly
            logger.warning("RenameBinCommand undo: old_name not stored")
            return project

        new_project = project.rename_bin(self.bin_id, self.old_name)
        logger.debug(f"RenameBinCommand undone: '{self.new_name}' -> '{self.old_name}'")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RenameBinCommand",
            "label": self.label,
            "bin_id": self.bin_id,
            "new_name": self.new_name,
            "old_name": self.old_name,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RenameBinCommand:
        """Deserialize command."""
        return RenameBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Rename Bin"),
            bin_id=data.get("bin_id", ""),
            new_name=data.get("new_name", ""),
            old_name=data.get("old_name"),
        )

    @staticmethod
    def create(bin_id: str, new_name: str) -> RenameBinCommand:
        """Factory method to create command."""
        return RenameBinCommand(
            command_id=str(uuid.uuid4()),
            label=f"Rename Bin to '{new_name}'",
            bin_id=bin_id,
            new_name=new_name,
        )


@dataclass(frozen=True)
class RenameAssetCommand:
    """
    Command to rename an asset's display name (does not touch disk file).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Asset ID to rename
        new_name: New display name
        old_name: Old display name (for undo, set during apply)
    """

    command_id: str
    label: str
    asset_id: str
    new_name: str
    old_name: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """Apply the command: rename asset display name."""
        asset = project.assets.get(self.asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {self.asset_id}")

        # Persist old name on this frozen command for undo (factory leaves it None).
        if self.old_name is None:
            object.__setattr__(self, "old_name", asset.display_name)
        stored_old_name = self.old_name

        new_project = project.rename_asset(self.asset_id, self.new_name)
        logger.debug(
            f"RenameAssetCommand applied: '{stored_old_name}' -> '{self.new_name}'"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """Undo the command: restore old display name."""
        if self.old_name is None:
            logger.warning("RenameAssetCommand undo: old_name not stored")
            return project
        new_project = project.rename_asset(self.asset_id, self.old_name)
        logger.debug(
            f"RenameAssetCommand undone: '{self.new_name}' -> '{self.old_name}'"
        )
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RenameAssetCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_name": self.new_name,
            "old_name": self.old_name,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RenameAssetCommand:
        """Deserialize command."""
        return RenameAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Rename Asset"),
            asset_id=data.get("asset_id", ""),
            new_name=data.get("new_name", ""),
            old_name=data.get("old_name"),
        )

    @staticmethod
    def create(asset_id: str, new_name: str) -> RenameAssetCommand:
        """Factory method to create command."""
        return RenameAssetCommand(
            command_id=str(uuid.uuid4()),
            label=f"Rename Asset to '{new_name}'",
            asset_id=asset_id,
            new_name=new_name,
        )


@dataclass(frozen=True)
class MoveBinCommand:
    """
    Command to reparent a bin (move it into another bin / to root).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        bin_id: Bin ID to move
        new_parent_id: Destination parent bin ID (root_bin_id = root level)
        old_parent_id: Previous parent (for undo, set during apply)
    """

    command_id: str
    label: str
    bin_id: str
    new_parent_id: str
    old_parent_id: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """Apply the command: reparent the bin."""
        bin_obj = project.bins.get(self.bin_id)
        if bin_obj is None:
            raise ValueError(f"Bin not found: {self.bin_id}")
        # Persist the old parent for undo (factory leaves it None).
        if self.old_parent_id is None:
            object.__setattr__(self, "old_parent_id", bin_obj.parent_id)
        new_project = project.reparent_bin(self.bin_id, self.new_parent_id)
        logger.debug(
            f"MoveBinCommand applied: bin {self.bin_id} -> parent {self.new_parent_id}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """Undo the command: restore the previous parent."""
        if self.old_parent_id is None:
            logger.warning("MoveBinCommand undo: old_parent_id not stored")
            return project
        new_project = project.reparent_bin(self.bin_id, self.old_parent_id)
        logger.debug(
            f"MoveBinCommand undone: bin {self.bin_id} -> parent {self.old_parent_id}"
        )
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveBinCommand",
            "label": self.label,
            "bin_id": self.bin_id,
            "new_parent_id": self.new_parent_id,
            "old_parent_id": self.old_parent_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveBinCommand:
        """Deserialize command."""
        return MoveBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Bin"),
            bin_id=data.get("bin_id", ""),
            new_parent_id=data.get("new_parent_id", ""),
            old_parent_id=data.get("old_parent_id"),
        )

    @staticmethod
    def create(bin_id: str, new_parent_id: str) -> MoveBinCommand:
        """Factory method to create command."""
        return MoveBinCommand(
            command_id=str(uuid.uuid4()),
            label="Move Bin",
            bin_id=bin_id,
            new_parent_id=new_parent_id,
        )


@dataclass(frozen=True)
class MoveAssetToBinCommand:
    """
    Command to move an asset to a bin.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Asset ID to move
        target_bin_id: Target bin ID
        source_bin_id: Source bin ID (for undo, set during apply)
    """

    command_id: str
    label: str
    asset_id: str
    target_bin_id: str
    source_bin_id: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: move asset to bin.

        Note: In the current design, assets can exist in multiple bins
        (they are references, not exclusive). This command adds the asset
        to the target bin without removing from source.

        Args:
            project: Current project

        Returns:
            New project with asset moved
        """
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if self.target_bin_id not in project.bins:
            raise ValueError(f"Target bin not found: {self.target_bin_id}")

        new_project = project.move_asset_to_bin(self.asset_id, self.target_bin_id)
        logger.debug(
            f"MoveAssetToBinCommand applied: asset {self.asset_id} "
            f"to bin {self.target_bin_id}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: remove asset from target bin.

        Note: Since asset is added (not moved exclusively), undo removes
        the reference from target bin.

        Args:
            project: Current project

        Returns:
            Project with asset removed from target bin
        """
        target_bin = project.bins.get(self.target_bin_id)
        if target_bin is None:
            logger.warning("MoveAssetToBinCommand undo: target bin not found")
            return project

        # Remove asset from target bin
        new_bin = target_bin.with_asset_removed(self.asset_id)
        new_bins = dict(project.bins)
        new_bins[self.target_bin_id] = new_bin

        import time as time_module
        # N20 修复：此处 replace 的对象是**工程**而非命令自身——旧代码
        # `replace(self, project_id=…)` 把工程字段塞进命令 dataclass，
        # 撤销该命令必然 TypeError（test_undo_move_asset 在 Mac/隔离态
        # 均崩）。对齐镜像 MoveTimelineToBinCommand.undo 的写法。
        new_project = replace(
            project, bins=new_bins,
            modified_at=time_module.time())

        logger.debug(f"MoveAssetToBinCommand undone: removed from {self.target_bin_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveAssetToBinCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "target_bin_id": self.target_bin_id,
            "source_bin_id": self.source_bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveAssetToBinCommand:
        """Deserialize command."""
        return MoveAssetToBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Asset to Bin"),
            asset_id=data.get("asset_id", ""),
            target_bin_id=data.get("target_bin_id", ""),
            source_bin_id=data.get("source_bin_id"),
        )

    @staticmethod
    def create(asset_id: str, target_bin_id: str) -> MoveAssetToBinCommand:
        """Factory method to create command."""
        return MoveAssetToBinCommand(
            command_id=str(uuid.uuid4()),
            label="Move Asset to Bin",
            asset_id=asset_id,
            target_bin_id=target_bin_id,
        )


@dataclass(frozen=True)
class MoveTimelineToBinCommand:
    """
    Command to add a timeline to a bin.

    Mirrors :class:`MoveAssetToBinCommand`. Timelines are references and can
    exist in multiple bins; ``apply`` adds the timeline to the target bin and
    ``undo`` removes that reference.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        timeline_id: Timeline ID to move
        target_bin_id: Target bin ID
    """

    command_id: str
    label: str
    timeline_id: str
    target_bin_id: str

    def apply(self, project: EditProject) -> EditProject:
        """Apply the command: add timeline to target bin."""
        if self.timeline_id not in project.timelines:
            raise ValueError(f"Timeline not found: {self.timeline_id}")
        if self.target_bin_id not in project.bins:
            raise ValueError(f"Target bin not found: {self.target_bin_id}")

        new_project = project.move_timeline_to_bin(self.timeline_id, self.target_bin_id)
        logger.debug(
            f"MoveTimelineToBinCommand applied: timeline {self.timeline_id} "
            f"to bin {self.target_bin_id}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """Undo the command: remove timeline from target bin."""
        target_bin = project.bins.get(self.target_bin_id)
        if target_bin is None:
            logger.warning("MoveTimelineToBinCommand undo: target bin not found")
            return project

        new_bin = target_bin.with_timeline_removed(self.timeline_id)
        new_bins = dict(project.bins)
        new_bins[self.target_bin_id] = new_bin

        import time as time_module
        new_project = replace(project, bins=new_bins, modified_at=time_module.time())
        logger.debug(f"MoveTimelineToBinCommand undone: removed from {self.target_bin_id}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveTimelineToBinCommand",
            "label": self.label,
            "timeline_id": self.timeline_id,
            "target_bin_id": self.target_bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveTimelineToBinCommand:
        """Deserialize command."""
        return MoveTimelineToBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Timeline to Bin"),
            timeline_id=data.get("timeline_id", ""),
            target_bin_id=data.get("target_bin_id", ""),
        )

    @staticmethod
    def create(timeline_id: str, target_bin_id: str) -> MoveTimelineToBinCommand:
        """Factory method to create command."""
        return MoveTimelineToBinCommand(
            command_id=str(uuid.uuid4()),
            label="Move Timeline to Bin",
            timeline_id=timeline_id,
            target_bin_id=target_bin_id,
        )


@dataclass(frozen=True)
class RemoveAssetFromBinCommand:
    """Command to remove an asset reference from a bin (drag out to root).

    The inverse of :class:`MoveAssetToBinCommand`. The asset stays in the
    project (still reachable via root), it just loses the bin reference.
    """

    command_id: str
    label: str
    asset_id: str
    bin_id: str

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if self.bin_id not in project.bins:
            raise ValueError(f"Bin not found: {self.bin_id}")
        new_project = project.remove_asset_from_bin(self.asset_id, self.bin_id)
        logger.debug(
            f"RemoveAssetFromBinCommand applied: asset {self.asset_id} from bin {self.bin_id}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        new_project = project.move_asset_to_bin(self.asset_id, self.bin_id)
        logger.debug(
            f"RemoveAssetFromBinCommand undone: asset {self.asset_id} back to bin {self.bin_id}"
        )
        return new_project

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "RemoveAssetFromBinCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "bin_id": self.bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RemoveAssetFromBinCommand:
        return RemoveAssetFromBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Asset from Bin"),
            asset_id=data.get("asset_id", ""),
            bin_id=data.get("bin_id", ""),
        )

    @staticmethod
    def create(asset_id: str, bin_id: str) -> RemoveAssetFromBinCommand:
        return RemoveAssetFromBinCommand(
            command_id=str(uuid.uuid4()),
            label="Remove Asset from Bin",
            asset_id=asset_id,
            bin_id=bin_id,
        )


@dataclass(frozen=True)
class RemoveTimelineFromBinCommand:
    """Command to remove a timeline reference from a bin (drag out to root)."""

    command_id: str
    label: str
    timeline_id: str
    bin_id: str

    def apply(self, project: EditProject) -> EditProject:
        if self.timeline_id not in project.timelines:
            raise ValueError(f"Timeline not found: {self.timeline_id}")
        if self.bin_id not in project.bins:
            raise ValueError(f"Bin not found: {self.bin_id}")
        new_project = project.remove_timeline_from_bin(self.timeline_id, self.bin_id)
        logger.debug(
            f"RemoveTimelineFromBinCommand applied: timeline {self.timeline_id} from bin {self.bin_id}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        new_project = project.move_timeline_to_bin(self.timeline_id, self.bin_id)
        logger.debug(
            f"RemoveTimelineFromBinCommand undone: timeline {self.timeline_id} back to bin {self.bin_id}"
        )
        return new_project

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "RemoveTimelineFromBinCommand",
            "label": self.label,
            "timeline_id": self.timeline_id,
            "bin_id": self.bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RemoveTimelineFromBinCommand:
        return RemoveTimelineFromBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Timeline from Bin"),
            timeline_id=data.get("timeline_id", ""),
            bin_id=data.get("bin_id", ""),
        )

    @staticmethod
    def create(timeline_id: str, bin_id: str) -> RemoveTimelineFromBinCommand:
        return RemoveTimelineFromBinCommand(
            command_id=str(uuid.uuid4()),
            label="Remove Timeline from Bin",
            timeline_id=timeline_id,
            bin_id=bin_id,
        )


def _registered_scene_id_for_asset(project: EditProject, asset_id: str) -> str | None:
    """返回随 ``asset_id`` 副本注册进共享 scene store 的 scene_id（AUD-17108）。

    ``EditProject.duplicate_asset`` 为 motion_graphic 素材克隆场景并注册进
    teaching_animation 的共享 store——该副作用发生在数据模型深处，撤销历史
    原本无法回收。命令层在 apply 后调用本帮助函数记录 scene_id，使 undo
    可以反注册。仅当 store 中确实存在该 scene 时返回 id（空场景回退分支
    与普通素材返回 None，undo 对其不做反注册）。
    """
    asset = project.assets.get(asset_id)
    if asset is None:
        return None
    if str(getattr(asset, "media_type", "")) != "motion_graphic":
        return None
    scene_id = str((getattr(asset, "metadata", {}) or {}).get("scene_id", "") or "")
    if not scene_id:
        return None
    try:
        from src.features.teaching_animation.scene_store import get_shared_scene_store
    except ImportError:
        return None
    if get_shared_scene_store().get(scene_id) is None:
        return None
    return scene_id


def _unregister_duplicated_scene(scene_id: str | None) -> None:
    """回滚 apply 期间对共享 scene store 的注册（AUD-17108 反注册半边）。"""
    if not scene_id:
        return
    try:
        from src.features.teaching_animation.scene_store import get_shared_scene_store
    except ImportError:
        return
    removed = get_shared_scene_store().remove(scene_id)
    if removed is not None:
        logger.debug(f"DuplicateAssetCommand undo: unregistered scene {scene_id}")


@dataclass(frozen=True)
class DuplicateAssetCommand:
    """Command to duplicate an asset (new id, shared source file).

    Used for Cmd+D "Duplicate" and clipboard paste. Undo removes the copy.
    """

    command_id: str
    label: str
    source_asset_id: str
    target_bin_id: str
    created_asset_id: str | None = None  # Set during apply
    # AUD-17108：apply 期间随 motion_graphic 副本注册进共享 scene store 的
    # scene_id；undo 反注册同一 id（apply/undo 注册/反注册对称）。
    created_scene_id: str | None = None

    def apply(self, project: EditProject) -> EditProject:
        if self.source_asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.source_asset_id}")
        if self.target_bin_id != project.root_bin_id and self.target_bin_id not in project.bins:
            raise ValueError(f"Target bin not found: {self.target_bin_id}")
        new_project, new_id = project.duplicate_asset(
            self.source_asset_id, self.target_bin_id
        )
        object.__setattr__(self, "created_asset_id", new_id)
        object.__setattr__(
            self,
            "created_scene_id",
            _registered_scene_id_for_asset(new_project, new_id),
        )
        logger.debug(
            f"DuplicateAssetCommand applied: {self.source_asset_id} -> {new_id}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        if self.created_asset_id is None:
            logger.warning("DuplicateAssetCommand undo: created_asset_id not stored")
            return project
        new_project = project
        if self.created_asset_id in project.assets:
            new_project = project.remove_asset(self.created_asset_id)
            logger.debug(f"DuplicateAssetCommand undone: removed {self.created_asset_id}")
        else:
            logger.debug(
                "DuplicateAssetCommand undo: asset already absent "
                f"({self.created_asset_id}); rolling back scene store only"
            )
        # AUD-17108：无论素材是否仍在，apply 注册的场景都必须反注册——
        # scene_id 由 apply 独占生成，不会与其他素材共享。
        _unregister_duplicated_scene(self.created_scene_id)
        return new_project

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "DuplicateAssetCommand",
            "label": self.label,
            "source_asset_id": self.source_asset_id,
            "target_bin_id": self.target_bin_id,
            "created_asset_id": self.created_asset_id,
            "created_scene_id": self.created_scene_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DuplicateAssetCommand:
        return DuplicateAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Duplicate Asset"),
            source_asset_id=data.get("source_asset_id", ""),
            target_bin_id=data.get("target_bin_id", ""),
            created_asset_id=data.get("created_asset_id"),
            created_scene_id=data.get("created_scene_id"),
        )

    @staticmethod
    def create(source_asset_id: str, target_bin_id: str) -> DuplicateAssetCommand:
        return DuplicateAssetCommand(
            command_id=str(uuid.uuid4()),
            label="Duplicate Asset",
            source_asset_id=source_asset_id,
            target_bin_id=target_bin_id,
        )


@dataclass(frozen=True)
class UpdateAssetProductionInfoCommand:
    """Command to edit an asset's Reel/Scene/Shot/Take metadata."""

    command_id: str
    label: str
    asset_id: str
    new_reel: str
    new_scene: str
    new_shot: str
    new_take: str
    old_reel: str = ""
    old_scene: str = ""
    old_shot: str = ""
    old_take: str = ""
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if not self._applied:
            asset = project.assets[self.asset_id]
            object.__setattr__(self, "old_reel", asset.reel)
            object.__setattr__(self, "old_scene", asset.scene)
            object.__setattr__(self, "old_shot", asset.shot)
            object.__setattr__(self, "old_take", asset.take)
            object.__setattr__(self, "_applied", True)
        return project.update_asset_production_info(
            self.asset_id,
            reel=self.new_reel,
            scene=self.new_scene,
            shot=self.new_shot,
            take=self.new_take,
        )

    def undo(self, project: EditProject) -> EditProject:
        return project.update_asset_production_info(
            self.asset_id,
            reel=self.old_reel,
            scene=self.old_scene,
            shot=self.old_shot,
            take=self.old_take,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateAssetProductionInfoCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_reel": self.new_reel,
            "new_scene": self.new_scene,
            "new_shot": self.new_shot,
            "new_take": self.new_take,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateAssetProductionInfoCommand:
        return UpdateAssetProductionInfoCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Edit Production Info"),
            asset_id=data.get("asset_id", ""),
            new_reel=data.get("new_reel", ""),
            new_scene=data.get("new_scene", ""),
            new_shot=data.get("new_shot", ""),
            new_take=data.get("new_take", ""),
        )

    @staticmethod
    def create(
        asset_id: str,
        reel: str,
        scene: str,
        shot: str,
        take: str,
    ) -> UpdateAssetProductionInfoCommand:
        return UpdateAssetProductionInfoCommand(
            command_id=str(uuid.uuid4()),
            label="Edit Production Info",
            asset_id=asset_id,
            new_reel=reel,
            new_scene=scene,
            new_shot=shot,
            new_take=take,
        )


@dataclass(frozen=True)
class UpdateProxyPolicyCommand:
    """Command to update the project-level proxy policy (P1-14).

    The policy is stored as a dict snapshot so the frozen command stays
    hashable and serializable; undo restores the previous policy (or the
    None default on projects that predate the field).
    """

    command_id: str
    label: str
    new_policy_dict: dict[str, Any]
    _old_policy_dict: dict[str, Any] | None = None
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        from src.features.edit.core.proxy_policy import ProxyPolicySettings
        if not self._applied:
            current = project.proxy_policy
            if hasattr(current, "to_dict"):
                old = current.to_dict()
            elif isinstance(current, dict):
                old = dict(current)
            else:
                old = None
            object.__setattr__(self, "_old_policy_dict", old)
            object.__setattr__(self, "_applied", True)
        return project.with_proxy_policy(
            ProxyPolicySettings.from_dict(self.new_policy_dict)
        )

    def undo(self, project: EditProject) -> EditProject:
        if not self._applied:
            logger.warning("UpdateProxyPolicyCommand undo before apply")
            return project
        from src.features.edit.core.proxy_policy import ProxyPolicySettings
        policy = (
            ProxyPolicySettings.from_dict(self._old_policy_dict)
            if self._old_policy_dict
            else None
        )
        return project.with_proxy_policy(policy)

    def redo(self, project: EditProject) -> EditProject:
        return self.apply(project)

    @staticmethod
    def create(policy: Any) -> "UpdateProxyPolicyCommand":
        data = policy.to_dict() if hasattr(policy, "to_dict") else dict(policy)
        return UpdateProxyPolicyCommand(
            command_id=str(uuid.uuid4()),
            label="Update Proxy Policy",
            new_policy_dict=data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize command (including undo state captured during apply)."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateProxyPolicyCommand",
            "label": self.label,
            "new_policy_dict": self.new_policy_dict,
            "old_policy_dict": self._old_policy_dict,
            "applied": self._applied,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateProxyPolicyCommand":
        """Deserialize command."""
        new_policy = data.get("new_policy_dict")
        return UpdateProxyPolicyCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Proxy Policy"),
            new_policy_dict=dict(new_policy) if isinstance(new_policy, dict) else {},
            _old_policy_dict=data.get("old_policy_dict"),
            _applied=bool(data.get("applied", False)),
        )


@dataclass(frozen=True)
class UpdateAssetLogCommand:
    """Command to edit an asset's log fields (P1-12: keywords/rating/flag/color/comment).

    None-valued new_* fields keep the asset's current value, matching
    ``EditProject.update_asset_log_info`` semantics. Undo restores the
    snapshot captured on the first apply.
    """

    command_id: str
    label: str
    asset_id: str
    new_keywords: tuple[str, ...] | None = None
    new_rating: int | None = None
    new_flag: str | None = None
    new_color_label: str | None = None
    new_comment: str | None = None
    _old_keywords: tuple[str, ...] = ()
    _old_rating: int = 0
    _old_flag: str = "none"
    _old_color_label: str = "none"
    _old_comment: str = ""
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if not self._applied:
            asset = project.assets[self.asset_id]
            object.__setattr__(self, "_old_keywords", asset.keywords)
            object.__setattr__(self, "_old_rating", asset.rating)
            object.__setattr__(self, "_old_flag", asset.flag)
            object.__setattr__(self, "_old_color_label", asset.color_label)
            object.__setattr__(self, "_old_comment", asset.comment)
            object.__setattr__(self, "_applied", True)
        return project.update_asset_log_info(
            self.asset_id,
            keywords=self.new_keywords,
            rating=self.new_rating,
            flag=self.new_flag,
            color_label=self.new_color_label,
            comment=self.new_comment,
        )

    def undo(self, project: EditProject) -> EditProject:
        if not self._applied:
            logger.warning("UpdateAssetLogCommand undo before apply")
            return project
        return project.update_asset_log_info(
            self.asset_id,
            keywords=self._old_keywords,
            rating=self._old_rating,
            flag=self._old_flag,
            color_label=self._old_color_label,
            comment=self._old_comment,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateAssetLogCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_keywords": list(self.new_keywords) if self.new_keywords is not None else None,
            "new_rating": self.new_rating,
            "new_flag": self.new_flag,
            "new_color_label": self.new_color_label,
            "new_comment": self.new_comment,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateAssetLogCommand:
        raw_keywords = data.get("new_keywords")
        return UpdateAssetLogCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Edit Asset Log"),
            asset_id=data.get("asset_id", ""),
            new_keywords=tuple(raw_keywords) if raw_keywords is not None else None,
            new_rating=data.get("new_rating"),
            new_flag=data.get("new_flag"),
            new_color_label=data.get("new_color_label"),
            new_comment=data.get("new_comment"),
        )

    @staticmethod
    def create(
        asset_id: str,
        *,
        keywords: tuple[str, ...] | None = None,
        rating: int | None = None,
        flag: str | None = None,
        color_label: str | None = None,
        comment: str | None = None,
        label: str = "Edit Asset Log",
    ) -> UpdateAssetLogCommand:
        return UpdateAssetLogCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            new_keywords=keywords,
            new_rating=rating,
            new_flag=flag,
            new_color_label=color_label,
            new_comment=comment,
        )


@dataclass(frozen=True)
class SetAssetInOutCommand:
    """P0-2: 设置/清除素材级 In/Out（源文件帧域，含端点）。

    项目域命令：apply/undo 收 EditProject；帧域校验由
    asset_source_range.set_asset_in_out 承担（倒置/负数/越界抛
    SourceRangeError，apply 失败不留半状态）。Undo 恢复首次 apply
    时快照的旧 In/Out（含"未设置"）。
    """

    command_id: str
    label: str
    asset_id: str
    new_in: int | None = None
    new_out: int | None = None
    _old_in: int | None = None
    _old_out: int | None = None
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        from src.features.edit.core.asset_source_range import (
            get_asset_in_out, set_asset_in_out,
        )
        if not self._applied:
            old_in, old_out = get_asset_in_out(project.assets[self.asset_id])
            object.__setattr__(self, "_old_in", old_in)
            object.__setattr__(self, "_old_out", old_out)
            object.__setattr__(self, "_applied", True)
        return set_asset_in_out(project, self.asset_id, self.new_in, self.new_out)

    def undo(self, project: EditProject) -> EditProject:
        from src.features.edit.core.asset_source_range import set_asset_in_out
        if not self._applied:
            logger.warning("SetAssetInOutCommand undo before apply")
            return project
        return set_asset_in_out(
            project, self.asset_id, self._old_in, self._old_out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetAssetInOutCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_in": self.new_in,
            "new_out": self.new_out,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetAssetInOutCommand":
        return SetAssetInOutCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Clip In/Out"),
            asset_id=data.get("asset_id", ""),
            new_in=data.get("new_in"),
            new_out=data.get("new_out"),
        )

    @staticmethod
    def create(
        asset_id: str,
        in_frame: int | None,
        out_frame: int | None,
        *,
        label: str = "Set Clip In/Out",
    ) -> "SetAssetInOutCommand":
        return SetAssetInOutCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            new_in=in_frame,
            new_out=out_frame,
        )


@dataclass(frozen=True)
class SetAssetMarkersCommand:
    """P0-2: 整体替换素材级标记列表。

    标记 schema/帧域校验由 asset_source_range.set_asset_markers 承担；
    undo 恢复首次 apply 时快照的旧列表。
    """

    command_id: str
    label: str
    asset_id: str
    new_markers: tuple[dict[str, Any], ...] = ()
    _old_markers: tuple[dict[str, Any], ...] = ()
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        from src.features.edit.core.asset_source_range import (
            list_asset_markers, set_asset_markers,
        )
        if not self._applied:
            object.__setattr__(
                self, "_old_markers",
                list_asset_markers(project.assets[self.asset_id]))
            object.__setattr__(self, "_applied", True)
        return set_asset_markers(
            project, self.asset_id, list(self.new_markers))

    def undo(self, project: EditProject) -> EditProject:
        from src.features.edit.core.asset_source_range import set_asset_markers
        if not self._applied:
            logger.warning("SetAssetMarkersCommand undo before apply")
            return project
        return set_asset_markers(
            project, self.asset_id, list(self._old_markers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetAssetMarkersCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_markers": [dict(m) for m in self.new_markers],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetAssetMarkersCommand":
        raw = data.get("new_markers") or []
        return SetAssetMarkersCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Asset Markers"),
            asset_id=data.get("asset_id", ""),
            new_markers=tuple(dict(m) for m in raw if isinstance(m, dict)),
        )

    @staticmethod
    def create(
        asset_id: str,
        markers: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        label: str = "Set Asset Markers",
    ) -> "SetAssetMarkersCommand":
        return SetAssetMarkersCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            new_markers=tuple(dict(m) for m in markers),
        )


@dataclass(frozen=True)
class CreateSubclipCommand:
    """P0-2: 从素材创建手工子片段（单个 Ctrl+Z 单元）。

    apply = 建子片段资产并挂入目标素材箱（None=root）；undo = 移除该
    子片段（remove_asset 会同步清理所有 bin 引用）。范围校验失败抛
    SourceRangeError，apply 不留半状态。
    """

    command_id: str
    label: str
    asset_id: str
    start_frame: int
    end_frame: int
    display_name: str | None = None
    target_bin_id: str | None = None
    _created_subclip_id: str | None = None
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        from src.features.edit.core.asset_source_range import create_manual_subclip
        # redo 重放时复用首次 apply 的稳定 ID：undo/redo 后 bin 引用与
        # 时间线引用（若有）按 asset_id 恢复一致。
        new_project, subclip = create_manual_subclip(
            project,
            self.asset_id,
            self.start_frame,
            self.end_frame,
            display_name=self.display_name,
            target_bin_id=self.target_bin_id,
            subclip_asset_id=self._created_subclip_id,
        )
        object.__setattr__(self, "_created_subclip_id", subclip.asset_id)
        object.__setattr__(self, "_applied", True)
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        if not self._applied or self._created_subclip_id is None:
            logger.warning("CreateSubclipCommand undo before apply")
            return project
        if self._created_subclip_id not in project.assets:
            logger.warning(
                f"CreateSubclipCommand undo: subclip {self._created_subclip_id} gone")
            return project
        return project.remove_asset(self._created_subclip_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "CreateSubclipCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "display_name": self.display_name,
            "target_bin_id": self.target_bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CreateSubclipCommand":
        return CreateSubclipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Create Subclip"),
            asset_id=data.get("asset_id", ""),
            start_frame=int(data.get("start_frame", 0)),
            end_frame=int(data.get("end_frame", 0)),
            display_name=data.get("display_name"),
            target_bin_id=data.get("target_bin_id"),
        )

    @staticmethod
    def create(
        asset_id: str,
        start_frame: int,
        end_frame: int,
        *,
        display_name: str | None = None,
        target_bin_id: str | None = None,
        label: str = "Create Subclip",
    ) -> "CreateSubclipCommand":
        return CreateSubclipCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            start_frame=start_frame,
            end_frame=end_frame,
            display_name=display_name,
            target_bin_id=target_bin_id,
        )


@dataclass(frozen=True)
class UpdateSourcePatchCommand:
    """F2.1: 设置/清除素材级 Source Patch 覆盖（V/A 流开关 + 源声道映射）。

    项目域命令（同 UpdateAssetLogCommand：apply/undo 收 EditProject）。
    ``new_spec`` 为 ``SourcePatchSpec``，None = 清除覆盖（回退默认直通）。
    Undo 恢复首次 apply 时快照的旧值。素材元数据已知声道数时在 apply
    即校验映射（越界/重复目标），而非等到渲染消费才失败。
    """

    command_id: str
    label: str
    asset_id: str
    new_spec: Any = None  # SourcePatchSpec | None
    _old_spec: Any = None  # SourcePatchSpec | None（快照，含"无覆盖"）
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if not self._applied:
            object.__setattr__(
                self, "_old_spec",
                project.source_patch_overrides.get(self.asset_id),
            )
            object.__setattr__(self, "_applied", True)
        if self.new_spec is not None:
            n_channels = project.assets[self.asset_id].audio_channels
            if n_channels:
                self.new_spec.validate(n_channels)
        return project.with_source_patch_override(self.asset_id, self.new_spec)

    def undo(self, project: EditProject) -> EditProject:
        if not self._applied:
            logger.warning("UpdateSourcePatchCommand undo before apply")
            return project
        return project.with_source_patch_override(self.asset_id, self._old_spec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateSourcePatchCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_spec": self.new_spec.to_dict() if self.new_spec is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateSourcePatchCommand":
        from src.features.edit.core.source_patch import SourcePatchSpec
        raw_spec = data.get("new_spec")
        return UpdateSourcePatchCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Source Patch"),
            asset_id=data.get("asset_id", ""),
            new_spec=(
                SourcePatchSpec.from_dict(raw_spec)
                if raw_spec is not None else None
            ),
        )

    @staticmethod
    def create(
        asset_id: str,
        spec: Any = None,
        *,
        label: str = "Source Patch",
    ) -> "UpdateSourcePatchCommand":
        return UpdateSourcePatchCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            new_spec=spec,
        )


@dataclass(frozen=True)
class UpdateAssetInterpretationCommand:
    """F2.7: 设置/清除素材级解释覆盖（fps 等，项目域命令）。

    override 为 ``InterpretationOverride``，None = 清除。apply 即校验
    （fps 范围）；apply 同时清素材代理状态（代理按探测参数生成，解释
    变更不重生成会串片）。undo 恢复覆盖与代理状态快照。
    """

    command_id: str
    label: str
    asset_id: str
    new_override: Any = None  # InterpretationOverride | None
    _old_override: Any = None
    _old_proxy_path: Any = None
    _old_proxy_status: str = "none"
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if not self._applied:
            asset = project.assets[self.asset_id]
            object.__setattr__(
                self, "_old_override",
                project.interpretation_overrides.get(self.asset_id))
            object.__setattr__(
                self, "_old_proxy_path", getattr(asset, "proxy_path", None))
            object.__setattr__(
                self, "_old_proxy_status",
                getattr(asset, "proxy_status", "none"))
            object.__setattr__(self, "_applied", True)
        if self.new_override is not None:
            self.new_override.validate()
        return project.with_interpretation_override(
            self.asset_id, self.new_override)

    def undo(self, project: EditProject) -> EditProject:
        if not self._applied:
            logger.warning("UpdateAssetInterpretationCommand undo before apply")
            return project
        restored = project.with_interpretation_override(
            self.asset_id, self._old_override)
        asset = restored.assets.get(self.asset_id)
        if asset is not None:
            from dataclasses import replace as _dc_replace
            restored = replace(
                restored, assets={
                    **restored.assets,
                    self.asset_id: _dc_replace(
                        asset, proxy_path=self._old_proxy_path,
                        proxy_status=self._old_proxy_status),
                })
        return restored

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateAssetInterpretationCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_override": (
                self.new_override.to_dict()
                if self.new_override is not None else None),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateAssetInterpretationCommand":
        from src.features.edit.core.interpretation import InterpretationOverride
        raw = data.get("new_override")
        return UpdateAssetInterpretationCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Interpret Media"),
            asset_id=data.get("asset_id", ""),
            new_override=(
                InterpretationOverride.from_dict(raw)
                if raw is not None else None),
        )

    @staticmethod
    def create(
        asset_id: str,
        override: Any = None,
        *,
        label: str = "Interpret Media",
    ) -> "UpdateAssetInterpretationCommand":
        return UpdateAssetInterpretationCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            new_override=override)


@dataclass(frozen=True)
class UpdateAudioSyncBindingCommand:
    """F2.10: 设置/清除视频素材的外录音频绑定（项目域命令）。

    apply 即校验 WAV 素材存在且为 audio 类型（不误绑）；undo 恢复
    首次快照。域区分：不是多机位角度偏移、不是 clip 链接、不碰播放
    时钟；原视频/WAV 文件零改写（非破坏）。
    """

    command_id: str
    label: str
    asset_id: str
    new_binding: Any = None  # AudioSyncBinding | None
    _old_binding: Any = None
    _applied: bool = False

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if not self._applied:
            object.__setattr__(
                self, "_old_binding",
                project.audio_sync_bindings.get(self.asset_id))
            object.__setattr__(self, "_applied", True)
        return project.with_audio_sync_binding(
            self.asset_id, self.new_binding)

    def undo(self, project: EditProject) -> EditProject:
        if not self._applied:
            logger.warning("UpdateAudioSyncBindingCommand undo before apply")
            return project
        return project.with_audio_sync_binding(
            self.asset_id, self._old_binding)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateAudioSyncBindingCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_binding": (
                self.new_binding.to_dict()
                if self.new_binding is not None else None),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateAudioSyncBindingCommand":
        from src.features.edit.core.audio_sync import AudioSyncBinding
        raw = data.get("new_binding")
        return UpdateAudioSyncBindingCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Bind External Audio"),
            asset_id=data.get("asset_id", ""),
            new_binding=(
                AudioSyncBinding.from_dict(raw)
                if raw is not None else None),
        )

    @staticmethod
    def create(
        asset_id: str,
        binding: Any = None,
        *,
        label: str = "Bind External Audio",
    ) -> "UpdateAudioSyncBindingCommand":
        return UpdateAudioSyncBindingCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_id=asset_id,
            new_binding=binding)


@dataclass(frozen=True)
class UpdateClipFpsInterpretationCommand:
    """F2.7: 既有片段按素材的帧率解释重锚（时间线域命令）。

    DaVinci "Use frame rate" 语义：**保持源帧窗不变**（source_start/
    source_duration 原样），仅改 fps 快照（_source_fps_num/den）并按新
    解释重算 timeline_duration（fade 钳到新时长）；undo 恢复完整快照。
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    new_fps_num: int
    new_fps_den: int
    _old_fps_num: int | None = None
    _old_fps_den: int | None = None
    _old_timeline_duration: int = 0
    _old_fade_in: int = 0
    _old_fade_out: int = 0
    _applied: bool = False

    def apply(self, timeline: Any) -> Any:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        assert_track_editable(timeline, self.track_id, "Reinterpret clip fps")
        if not self._applied:
            object.__setattr__(self, "_old_fps_num", clip._source_fps_num)
            object.__setattr__(self, "_old_fps_den", clip._source_fps_den)
            object.__setattr__(
                self, "_old_timeline_duration", clip.timeline_duration)
            object.__setattr__(self, "_old_fade_in", clip.fade_in_duration)
            object.__setattr__(self, "_old_fade_out", clip.fade_out_duration)
            object.__setattr__(self, "_applied", True)

        seq_fps = getattr(timeline, "fps", None)
        seq_num = seq_fps.num if hasattr(seq_fps, "num") else None
        seq_den = seq_fps.den if hasattr(seq_fps, "den") else 1
        from src.features.edit.core.timeline_ops import (
            timeline_duration_for_source,
        )
        new_duration = timeline_duration_for_source(
            clip.source_duration, clip.speed,
            self.new_fps_num, self.new_fps_den,
            clip._timeline_fps_num or seq_num,
            clip._timeline_fps_den or seq_den,
        )
        new_clip = replace(
            clip,
            _source_fps_num=self.new_fps_num,
            _source_fps_den=self.new_fps_den,
            timeline_duration=new_duration,
            fade_in_duration=min(clip.fade_in_duration, new_duration),
            fade_out_duration=min(clip.fade_out_duration, new_duration),
        )
        return timeline.with_track_updated(
            track.with_clip_updated(new_clip))

    def undo(self, timeline: Any) -> Any:
        if not self._applied:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        restored = replace(
            clip,
            _source_fps_num=self._old_fps_num,
            _source_fps_den=self._old_fps_den,
            timeline_duration=self._old_timeline_duration,
            fade_in_duration=self._old_fade_in,
            fade_out_duration=self._old_fade_out,
        )
        return timeline.with_track_updated(
            track.with_clip_updated(restored))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateClipFpsInterpretationCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "new_fps_num": self.new_fps_num,
            "new_fps_den": self.new_fps_den,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateClipFpsInterpretationCommand":
        return UpdateClipFpsInterpretationCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Reinterpret clip fps"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            new_fps_num=int(data.get("new_fps_num", 0)),
            new_fps_den=int(data.get("new_fps_den", 1)),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        fps_num: int,
        fps_den: int,
        *,
        label: str = "Reinterpret clip fps",
    ) -> "UpdateClipFpsInterpretationCommand":
        return UpdateClipFpsInterpretationCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            track_id=track_id,
            clip_id=clip_id,
            new_fps_num=int(fps_num),
            new_fps_den=int(fps_den) or 1,
        )


@dataclass(frozen=True)
class BatchUpdateAssetLogCommand:
    """Command to batch-edit log fields across many assets as one undo unit (P1-12).

    Keyword operations are merge-based per asset (add_keywords appends without
    duplicates, remove_keywords filters), while rating/flag/color/comment use
    direct overwrite. None / empty means "leave unchanged".

    Each apply rebuilds per-asset sub-commands from the *current* project
    state, so apply → undo → apply cycles capture correct snapshots.
    """

    command_id: str
    label: str
    asset_ids: tuple[str, ...]
    add_keywords: tuple[str, ...] = ()
    remove_keywords: tuple[str, ...] = ()
    rating: int | None = None
    flag: str | None = None
    color_label: str | None = None
    comment: str | None = None
    _last_subs: tuple = ()

    def apply(self, project: EditProject) -> EditProject:
        subs: list[UpdateAssetLogCommand] = []
        for asset_id in self.asset_ids:
            if asset_id not in project.assets:
                logger.warning(
                    "BatchUpdateAssetLogCommand: skip missing asset %s", asset_id
                )
                continue
            asset = project.assets[asset_id]
            keywords: tuple[str, ...] | None = None
            if self.add_keywords or self.remove_keywords:
                merged = list(asset.keywords)
                for kw in self.add_keywords:
                    if kw not in merged:
                        merged.append(kw)
                remove = set(self.remove_keywords)
                keywords = tuple(kw for kw in merged if kw not in remove)
            subs.append(
                UpdateAssetLogCommand.create(
                    asset_id,
                    keywords=keywords,
                    rating=self.rating,
                    flag=self.flag,
                    color_label=self.color_label,
                    comment=self.comment,
                    label=self.label,
                )
            )
        object.__setattr__(self, "_last_subs", tuple(subs))

        current = project
        for cmd in subs:
            current = cmd.apply(current)
        logger.debug(
            "BatchUpdateAssetLogCommand applied: %s (%d assets)",
            self.label, len(subs),
        )
        return current

    def undo(self, project: EditProject) -> EditProject:
        current = project
        for cmd in reversed(self._last_subs):
            current = cmd.undo(current)
        return current

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "BatchUpdateAssetLogCommand",
            "label": self.label,
            "asset_ids": list(self.asset_ids),
            "add_keywords": list(self.add_keywords),
            "remove_keywords": list(self.remove_keywords),
            "rating": self.rating,
            "flag": self.flag,
            "color_label": self.color_label,
            "comment": self.comment,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BatchUpdateAssetLogCommand:
        def _opt_str(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        rating = data.get("rating")
        return BatchUpdateAssetLogCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Batch Edit Asset Log"),
            asset_ids=tuple(data.get("asset_ids", ())),
            add_keywords=tuple(data.get("add_keywords", ())),
            remove_keywords=tuple(data.get("remove_keywords", ())),
            rating=int(rating) if isinstance(rating, int) else None,
            flag=_opt_str(data.get("flag")),
            color_label=_opt_str(data.get("color_label")),
            comment=_opt_str(data.get("comment")),
        )

    @staticmethod
    def create(
        asset_ids: tuple[str, ...] | str,
        *,
        add_keywords: tuple[str, ...] = (),
        remove_keywords: tuple[str, ...] = (),
        rating: int | None = None,
        flag: str | None = None,
        color_label: str | None = None,
        comment: str | None = None,
        label: str = "Batch Edit Asset Log",
    ) -> BatchUpdateAssetLogCommand:
        ids = (asset_ids,) if isinstance(asset_ids, str) else tuple(asset_ids)
        return BatchUpdateAssetLogCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            asset_ids=ids,
            add_keywords=tuple(add_keywords),
            remove_keywords=tuple(remove_keywords),
            rating=rating,
            flag=flag,
            color_label=color_label,
            comment=comment,
        )


@dataclass(frozen=True)
class CreateSmartBinCommand:
    """Command to create a user smart bin / saved search (P1-12).

    Undo removes it again; after undo the id is free, so redo works.
    """

    command_id: str
    label: str
    smart_bin: SmartBin

    def apply(self, project: EditProject) -> EditProject:
        return project.add_smart_bin(self.smart_bin)

    def undo(self, project: EditProject) -> EditProject:
        return project.remove_smart_bin(self.smart_bin.bin_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "CreateSmartBinCommand",
            "label": self.label,
            "smart_bin": {
                "bin_id": self.smart_bin.bin_id,
                "name": self.smart_bin.name,
                "filter_type": self.smart_bin.filter_type,
                "filter_params": dict(self.smart_bin.filter_params),
            },
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CreateSmartBinCommand:
        bin_data = data.get("smart_bin", {})
        return CreateSmartBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Create Smart Bin"),
            smart_bin=SmartBin(
                bin_id=bin_data.get("bin_id", ""),
                name=bin_data.get("name", ""),
                filter_type=bin_data.get("filter_type", "custom"),
                filter_params=dict(bin_data.get("filter_params", {})),
            ),
        )

    @staticmethod
    def create(smart_bin: SmartBin) -> CreateSmartBinCommand:
        return CreateSmartBinCommand(
            command_id=str(uuid.uuid4()),
            label=f"Create Smart Bin '{smart_bin.name}'",
            smart_bin=smart_bin,
        )


@dataclass(frozen=True)
class DeleteSmartBinCommand:
    """Command to delete a user smart bin (P1-12). Undo restores it."""

    command_id: str
    label: str
    bin_id: str
    _removed_bin: SmartBin | None = None

    def apply(self, project: EditProject) -> EditProject:
        if self._removed_bin is None:
            existing = project.smart_bins.get(self.bin_id)
            if existing is None:
                raise ValueError(f"Smart bin not found: {self.bin_id}")
            object.__setattr__(self, "_removed_bin", existing)
        return project.remove_smart_bin(self.bin_id)

    def undo(self, project: EditProject) -> EditProject:
        if self._removed_bin is None:
            logger.warning("DeleteSmartBinCommand undo before apply")
            return project
        return project.remove_smart_bin(self.bin_id).add_smart_bin(self._removed_bin)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "DeleteSmartBinCommand",
            "label": self.label,
            "bin_id": self.bin_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteSmartBinCommand:
        return DeleteSmartBinCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Smart Bin"),
            bin_id=data.get("bin_id", ""),
        )

    @staticmethod
    def create(bin_id: str) -> DeleteSmartBinCommand:
        return DeleteSmartBinCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Smart Bin",
            bin_id=bin_id,
        )


# ================================================================
# P6.3 Offline/Relink/Proxy Commands
# ================================================================

@dataclass(frozen=True)
class ReplaceMediaCommand:
    """Command to replace an asset's source file, keeping clip parameters (P1-13).

    The asset_id is unchanged so every timeline clip referencing the asset
    keeps its in/out points and effects. Undo restores the complete original
    MediaAsset (path/offline/proxy/metadata/display name) from a snapshot.
    """

    command_id: str
    label: str
    asset_id: str
    new_path: str
    new_metadata: dict[str, Any] | None = None
    # F-U04：探测到的新源类型（None = 沿用旧值）
    new_media_type: str | None = None
    # N22/RF06：新源色彩元数据（导入同口径提取；None = 沿用旧值）
    color_metadata: dict[str, Any] | None = None
    _old_asset: MediaAsset | None = None

    def apply(self, project: EditProject) -> EditProject:
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")
        if self._old_asset is None:
            object.__setattr__(self, "_old_asset", project.assets[self.asset_id])
        return replace_media(
            project,
            self.asset_id,
            self.new_path,
            new_metadata=self.new_metadata,
            new_media_type=self.new_media_type,
            color_metadata=self.color_metadata,
        )

    def undo(self, project: EditProject) -> EditProject:
        if self._old_asset is None:
            logger.warning("ReplaceMediaCommand undo before apply")
            return project
        asset = self._old_asset
        new_assets = dict(project.assets)
        new_assets[self.asset_id] = asset
        return replace(project, assets=new_assets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ReplaceMediaCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_path": self.new_path,
            "new_metadata": self.new_metadata,
            # N22/RF06：类型/色彩必须随载荷往返——此前 JSON 回放把
            # image 换源退回 video（新类型字段未序列化）。
            "new_media_type": self.new_media_type,
            "color_metadata": self.color_metadata,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ReplaceMediaCommand:
        return ReplaceMediaCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Replace Media"),
            asset_id=data.get("asset_id", ""),
            new_path=data.get("new_path", ""),
            new_metadata=data.get("new_metadata"),
            new_media_type=data.get("new_media_type"),
            color_metadata=data.get("color_metadata"),
        )

    @staticmethod
    def create(
        asset_id: str,
        new_path: str,
        *,
        new_metadata: dict[str, Any] | None = None,
        new_media_type: str | None = None,
        color_metadata: dict[str, Any] | None = None,
    ) -> ReplaceMediaCommand:
        return ReplaceMediaCommand(
            command_id=str(uuid.uuid4()),
            label=f"Replace Media '{asset_id}'",
            asset_id=asset_id,
            new_path=new_path,
            new_metadata=new_metadata,
            new_media_type=new_media_type,
            color_metadata=color_metadata,
        )

@dataclass(frozen=True)
class RelinkAssetCommand:
    """
    Command to relink an asset to a new file path.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Asset ID to relink
        new_path: New file path
        old_path: Old file path (for undo, set during apply)
    """

    command_id: str
    label: str
    asset_id: str
    new_path: str
    old_path: str | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: relink asset.

        Args:
            project: Current project

        Returns:
            New project with asset relinked
        """
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")

        asset = project.assets[self.asset_id]
        old_path = asset.path

        new_project = relink_asset(project, self.asset_id, self.new_path)
        logger.debug(
            f"RelinkAssetCommand applied: {self.asset_id} "
            f"from {old_path} to {self.new_path}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: restore old path.

        Args:
            project: Current project

        Returns:
            Project with asset path restored
        """
        if self.old_path is None:
            logger.warning("RelinkAssetCommand undo: old_path not stored")
            return project

        new_project = relink_asset(project, self.asset_id, self.old_path)
        logger.debug(f"RelinkAssetCommand undone: {self.asset_id} restored to {self.old_path}")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RelinkAssetCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "new_path": self.new_path,
            "old_path": self.old_path,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RelinkAssetCommand:
        """Deserialize command."""
        return RelinkAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Relink Asset"),
            asset_id=data.get("asset_id", ""),
            new_path=data.get("new_path", ""),
            old_path=data.get("old_path"),
        )

    @staticmethod
    def create(asset_id: str, new_path: str) -> RelinkAssetCommand:
        """Factory method to create command."""
        return RelinkAssetCommand(
            command_id=str(uuid.uuid4()),
            label=f"Relink Asset '{asset_id}'",
            asset_id=asset_id,
            new_path=new_path,
        )


@dataclass(frozen=True)
class BatchRelinkAssetsCommand:
    """
    Command to batch relink multiple assets.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        relink_map: {asset_id: new_path} mapping
        _old_paths: Internal: {asset_id: old_path} for undo (set during apply)
    """

    command_id: str
    label: str
    relink_map: dict[str, str]
    _old_paths: dict[str, str] | None = None  # Set during apply

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: batch relink assets.

        Args:
            project: Current project

        Returns:
            New project with assets relinked
        """
        # Store old paths for undo
        old_paths: dict[str, str] = {}
        for asset_id in self.relink_map:
            if asset_id in project.assets:
                old_paths[asset_id] = project.assets[asset_id].path

        new_project = batch_relink_assets(project, self.relink_map)
        logger.debug(f"BatchRelinkAssetsCommand applied: {len(self.relink_map)} assets")
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: restore old paths.

        Args:
            project: Current project

        Returns:
            Project with paths restored
        """
        if self._old_paths is None:
            logger.warning("BatchRelinkAssetsCommand undo: old_paths not stored")
            return project

        # Reverse the relink using old paths
        reverse_map = {asset_id: old_path for asset_id, old_path in self._old_paths.items()}
        new_project = batch_relink_assets(project, reverse_map)
        logger.debug(f"BatchRelinkAssetsCommand undone: {len(reverse_map)} assets restored")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "BatchRelinkAssetsCommand",
            "label": self.label,
            "relink_map": self.relink_map,
            "_old_paths": self._old_paths,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BatchRelinkAssetsCommand:
        """Deserialize command."""
        return BatchRelinkAssetsCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Batch Relink Assets"),
            relink_map=data.get("relink_map", {}),
            _old_paths=data.get("_old_paths"),
        )

    @staticmethod
    def create(relink_map: dict[str, str]) -> BatchRelinkAssetsCommand:
        """Factory method to create command."""
        return BatchRelinkAssetsCommand(
            command_id=str(uuid.uuid4()),
            label=f"Batch Relink {len(relink_map)} Assets",
            relink_map=relink_map,
        )


@dataclass(frozen=True)
class UpdateAssetProxyCommand:
    """
    Command to update asset proxy information.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        asset_id: Asset ID
        proxy_path: New proxy path
        proxy_status: New proxy status
        old_proxy_path: Old proxy path (for undo)
        old_proxy_status: Old proxy status (for undo)
    """

    command_id: str
    label: str
    asset_id: str
    proxy_path: str | None
    proxy_status: str
    old_proxy_path: str | None = None
    old_proxy_status: str | None = None

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply the command: update proxy info.

        Args:
            project: Current project

        Returns:
            New project with proxy info updated
        """
        if self.asset_id not in project.assets:
            raise ValueError(f"Asset not found: {self.asset_id}")


        new_project = update_asset_proxy(
            project, self.asset_id, self.proxy_path, self.proxy_status
        )
        logger.debug(
            f"UpdateAssetProxyCommand applied: {self.asset_id} "
            f"proxy_status={self.proxy_status}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo the command: restore old proxy info.

        Args:
            project: Current project

        Returns:
            Project with proxy info restored
        """
        if self.old_proxy_status is None:
            logger.warning("UpdateAssetProxyCommand undo: old status not stored")
            return project

        new_project = update_asset_proxy(
            project, self.asset_id, self.old_proxy_path, self.old_proxy_status
        )
        logger.debug(f"UpdateAssetProxyCommand undone: {self.asset_id} restored")
        return new_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateAssetProxyCommand",
            "label": self.label,
            "asset_id": self.asset_id,
            "proxy_path": self.proxy_path,
            "proxy_status": self.proxy_status,
            "old_proxy_path": self.old_proxy_path,
            "old_proxy_status": self.old_proxy_status,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateAssetProxyCommand:
        """Deserialize command."""
        return UpdateAssetProxyCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Asset Proxy"),
            asset_id=data.get("asset_id", ""),
            proxy_path=data.get("proxy_path"),
            proxy_status=data.get("proxy_status", "none"),
            old_proxy_path=data.get("old_proxy_path"),
            old_proxy_status=data.get("old_proxy_status"),
        )

    @staticmethod
    def create(
        asset_id: str,
        proxy_path: str | None,
        proxy_status: str
    ) -> UpdateAssetProxyCommand:
        """Factory method to create command."""
        return UpdateAssetProxyCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Proxy for '{asset_id}'",
            asset_id=asset_id,
            proxy_path=proxy_path,
            proxy_status=proxy_status,
        )


# ================================================================
# ImportMediaAssetCommand - P1-10 录音 take 导入（可撤销）
# ================================================================

@dataclass(frozen=True)
class ImportMediaAssetCommand:
    """Import a media file as a new project asset (single undo unit).

    P1-10 验收"R/停止只产生一个 asset 和一次 Undo"：录音 take 的导入
    走本命令进 ProjectCommandStack，Ctrl+Z 一次即可移除 take asset。
    路径已存在（重复导入）时为幂等 no-op，undo 也不会误删他人 asset。
    """

    command_id: str
    label: str
    path: str
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    display_name: str | None = None
    # apply 时记录：本次导入产生的 asset_id；None = 去重 no-op
    _imported_asset_id: str | None = None

    def apply(self, project: EditProject) -> EditProject:
        existing = project.get_asset_by_path(self.path)
        if existing is not None:
            object.__setattr__(self, "_imported_asset_id", None)
            return project
        new_project = project.import_asset(
            path=self.path,
            media_type=self.media_type,
            metadata=dict(self.metadata),
            display_name=self.display_name,
        )
        created = new_project.get_asset_by_path(self.path)
        object.__setattr__(
            self, "_imported_asset_id",
            created.asset_id if created is not None else None,
        )
        logger.debug(
            "ImportMediaAssetCommand applied: %s -> asset %s",
            self.path, self._imported_asset_id,
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject:
        if self._imported_asset_id is None:
            return project
        undone = project.remove_asset(self._imported_asset_id)
        logger.debug(
            "ImportMediaAssetCommand undone: removed asset %s",
            self._imported_asset_id,
        )
        return undone

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ImportMediaAssetCommand",
            "label": self.label,
            "path": self.path,
            "media_type": self.media_type,
            "metadata": dict(self.metadata),
            "display_name": self.display_name,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ImportMediaAssetCommand:
        return ImportMediaAssetCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Import Media"),
            path=data.get("path", ""),
            media_type=data.get("media_type", "video"),
            metadata=dict(data.get("metadata", {})),
            display_name=data.get("display_name"),
        )

    @staticmethod
    def create(
        path: str,
        media_type: str,
        metadata: dict[str, Any] | None = None,
        display_name: str | None = None,
        label: str = "Import Media",
    ) -> ImportMediaAssetCommand:
        return ImportMediaAssetCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            path=path,
            media_type=media_type,
            metadata=dict(metadata or {}),
            display_name=display_name,
        )


# ================================================================
# MarkBestTakeCommand - P1-10 最佳 take 标记（可撤销）
# ================================================================

@dataclass(frozen=True)
class MarkBestTakeCommand:
    """Mark one recording take asset as the best take of its scene.

    同场次（metadata['scene']）的其他 take 的 ``is_best_take`` 标记被
    清除；undo 恢复所有受影响 asset 的先前标记状态。
    """

    command_id: str
    label: str
    asset_id: str
    # apply 时记录：受影响 asset 的先前标记（asset_id, was_best）
    _previous_flags: tuple[tuple[str, bool], ...] = ()

    def apply(self, project: EditProject) -> EditProject:
        asset = project.assets.get(self.asset_id)
        if asset is None:
            logger.warning("MarkBestTakeCommand: asset %s not found", self.asset_id)
            return project
        scene = str(asset.metadata.get("scene", ""))
        affected: list[tuple[str, bool]] = []
        new_assets = dict(project.assets)
        for aid, other in project.assets.items():
            was_best = bool(other.metadata.get("is_best_take", False))
            if aid == self.asset_id:
                if was_best:
                    continue  # 已是最佳，无需变更
                affected.append((aid, was_best))
                new_assets[aid] = replace(other, metadata={
                    **other.metadata, "is_best_take": True})
            elif (was_best
                  and str(other.metadata.get("scene", "")) == scene):
                affected.append((aid, was_best))
                stripped = dict(other.metadata)
                stripped.pop("is_best_take", None)
                new_assets[aid] = replace(other, metadata=stripped)
        object.__setattr__(self, "_previous_flags", tuple(affected))
        if not affected:
            return project
        import time as time_module
        logger.debug("MarkBestTakeCommand applied: best=%s scene=%r",
                     self.asset_id, scene)
        return replace(project, assets=new_assets,
                       modified_at=time_module.time())

    def undo(self, project: EditProject) -> EditProject:
        if not self._previous_flags:
            return project
        new_assets = dict(project.assets)
        for asset_id, was_best in self._previous_flags:
            asset = new_assets.get(asset_id)
            if asset is None:
                continue
            metadata = dict(asset.metadata)
            if was_best:
                metadata["is_best_take"] = True
            else:
                metadata.pop("is_best_take", None)
            new_assets[asset_id] = replace(asset, metadata=metadata)
        import time as time_module
        logger.debug("MarkBestTakeCommand undone: %d asset(s) restored",
                     len(self._previous_flags))
        return replace(project, assets=new_assets,
                       modified_at=time_module.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "MarkBestTakeCommand",
            "label": self.label,
            "asset_id": self.asset_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MarkBestTakeCommand:
        return MarkBestTakeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Mark Best Take"),
            asset_id=data.get("asset_id", ""),
        )

    @staticmethod
    def create(asset_id: str) -> MarkBestTakeCommand:
        return MarkBestTakeCommand(
            command_id=str(uuid.uuid4()),
            label="Mark Best Take",
            asset_id=asset_id,
        )


@dataclass(frozen=True)
class UpdateTimelineSettingsCommand:
    """U01：时间线设置（含 FPS 时基变换）整体事务命令。

    before/after 携带完整序列 payload（三轨组、markers、master/track
    automation、字幕 cue/word timings、Bezier handles——由调用方经统一
    timebase transform 生成）与 timeline 引用元数据。apply/undo 原子
    交换：一步 Undo 恢复全部时间域；发布 revision 单调递增（undo 恢复
    内容但不回退 revision，旧 AI plan 保持冲突）。

    注：序列 payload 的缓存归属在控制器（EditProject.timelines 只存
    引用），命令 apply 返回 (project, sequence) 对由调用方落位；完整
    commit adapter 统一接线见复查计划 R25/U01。
    """

    command_id: str
    label: str
    timeline_id: str
    before_sequence: Any
    after_sequence: Any

    def _bump(self, base_revision: int) -> int:
        return int(base_revision) + 1

    def apply(self, project: EditProject) -> ProjectTransactionResult:
        """N20/RF04：返回结构化 ProjectTransactionResult（不再是裸
        tuple——ProjectCommandStack 此前把三元组直接 replace() 抛
        TypeError，设置事务从不能经真实栈提交）。内容快照不带发布
        revision，消费方按当前序列单调 +1。"""
        ref = project.timelines.get(self.timeline_id)
        if ref is None:
            raise ValueError(f"Timeline not found: {self.timeline_id}")
        updated_sequence = self.after_sequence
        new_project = self._project_with_ref(project, updated_sequence)
        return ProjectTransactionResult(
            project=new_project,
            sequence_map={self.timeline_id: updated_sequence},
            updated_command=self,
        )

    def _project_with_ref(self, project: EditProject, sequence) -> EditProject:
        ref = project.timelines.get(self.timeline_id)
        updated_refs = dict(project.timelines)
        updated_refs[self.timeline_id] = replace(
            ref,
            name=sequence.name,
            fps=float(sequence.fps),
            width=sequence.width,
            height=sequence.height,
            sample_rate=sequence.sample_rate,
            format_settings=dict(
                getattr(sequence, "format_settings", {}) or {}
            ),
        )
        return replace(project, timelines=updated_refs)

    def undo(self, project: EditProject) -> ProjectTransactionResult:
        ref = project.timelines.get(self.timeline_id)
        if ref is None:
            raise ValueError(f"Timeline not found: {self.timeline_id}")
        restored_sequence = self.before_sequence
        new_project = self._project_with_ref(project, restored_sequence)
        return ProjectTransactionResult(
            project=new_project,
            sequence_map={self.timeline_id: restored_sequence},
            updated_command=self,
        )

    @staticmethod
    def create(timeline_id: str, before_sequence, after_sequence):
        return UpdateTimelineSettingsCommand(
            command_id=str(uuid.uuid4()),
            label=(
                f"Update timeline settings "
                f"({float(after_sequence.fps):g}fps)"
            ),
            timeline_id=timeline_id,
            before_sequence=before_sequence,
            after_sequence=after_sequence,
        )
