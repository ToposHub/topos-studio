"""Marker commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: markers cluster (add/delete/move/
edit timeline markers). Class sources are byte-identical to the original
module; to_dict/from_dict keys are a persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import uuid

from src.features.edit.core.timeline_v2 import TimelineSequence, TimelineMarker

logger = logging.getLogger(__name__)


# ================================================================
# Marker Commands (Phase 5.1)
# ================================================================

@dataclass(frozen=True)
class AddMarkerCommand:
    """
    Command to add a marker to timeline.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        marker: Marker to add
        _previous_markers: Internal: markers before add (for undo)
    """

    command_id: str
    label: str
    marker: TimelineMarker
    _previous_markers: tuple[TimelineMarker, ...] | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: add marker to timeline.

        Args:
            timeline: Current timeline

        Returns:
            New timeline with marker added
        """
        # Store previous markers for undo
        object.__setattr__(self, '_previous_markers', timeline.markers)

        # Add marker
        new_markers = timeline.markers + (self.marker,)

        # Preserve all timeline fields (subtitle tracks, ranges, mixer state,
        # and future fields) while changing only the marker collection.
        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"AddMarkerCommand applied: {self.marker.marker_id} at frame {self.marker.frame}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove the marker.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the marker
        """
        if self._previous_markers is None:
            # Remove the marker directly
            new_markers = tuple(m for m in timeline.markers if m.marker_id != self.marker.marker_id)
        else:
            new_markers = self._previous_markers

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"AddMarkerCommand undone: removed {self.marker.marker_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "AddMarkerCommand",
            "label": self.label,
            "marker": self.marker.to_dict(),
            "_previous_markers": [m.to_dict() for m in self._previous_markers] if self._previous_markers else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddMarkerCommand:
        """Deserialize command."""
        marker_data = data.get("marker", {})
        marker = TimelineMarker.from_dict(marker_data)

        prev_markers_data = data.get("_previous_markers")
        prev_markers = tuple(TimelineMarker.from_dict(m) for m in prev_markers_data) if prev_markers_data else None

        return AddMarkerCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Marker"),
            marker=marker,
            _previous_markers=prev_markers,
        )

    @staticmethod
    def create(marker: TimelineMarker) -> AddMarkerCommand:
        """Factory method to create command."""
        return AddMarkerCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add Marker '{marker.name}' at {marker.frame}",
            marker=marker,
        )


@dataclass(frozen=True)
class DeleteMarkerCommand:
    """
    Command to delete a marker from timeline.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        marker_id: Marker ID to delete
        _deleted_marker: Internal: marker data for undo (set during apply)
    """

    command_id: str
    label: str
    marker_id: str
    _deleted_marker: TimelineMarker | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: delete marker.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the marker
        """
        # Find marker to delete
        marker_to_delete = None
        for m in timeline.markers:
            if m.marker_id == self.marker_id:
                marker_to_delete = m
                break

        if marker_to_delete is None:
            raise ValueError(f"Marker not found: {self.marker_id}")

        # Store for undo
        object.__setattr__(self, '_deleted_marker', marker_to_delete)

        # Remove marker
        new_markers = tuple(m for m in timeline.markers if m.marker_id != self.marker_id)

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"DeleteMarkerCommand applied: removed {self.marker_id}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore deleted marker.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with marker restored
        """
        if self._deleted_marker is None:
            raise ValueError("Cannot undo: deleted marker not stored")

        new_markers = timeline.markers + (self._deleted_marker,)

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"DeleteMarkerCommand undone: restored {self.marker_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "DeleteMarkerCommand",
            "label": self.label,
            "marker_id": self.marker_id,
            "_deleted_marker": self._deleted_marker.to_dict() if self._deleted_marker else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteMarkerCommand:
        """Deserialize command."""
        deleted_marker_data = data.get("_deleted_marker")
        deleted_marker = TimelineMarker.from_dict(deleted_marker_data) if deleted_marker_data else None

        return DeleteMarkerCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Marker"),
            marker_id=data.get("marker_id", ""),
            _deleted_marker=deleted_marker,
        )

    @staticmethod
    def create(marker_id: str) -> DeleteMarkerCommand:
        """Factory method to create command."""
        return DeleteMarkerCommand(
            command_id=str(uuid.uuid4()),
            label=f"Delete Marker {marker_id[:8]}",
            marker_id=marker_id,
        )


@dataclass(frozen=True)
class MoveMarkerCommand:
    """
    Command to move a marker to a new frame position.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        marker_id: Marker ID to move
        new_frame: New frame position
        old_frame: Old frame position (for undo)
        _old_marker: Internal: old marker data (for undo)
    """

    command_id: str
    label: str
    marker_id: str
    new_frame: int
    old_frame: int | None = None
    _old_marker: TimelineMarker | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: move marker to new frame.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with marker moved
        """
        # Find marker
        old_marker = None
        for m in timeline.markers:
            if m.marker_id == self.marker_id:
                old_marker = m
                break

        if old_marker is None:
            raise ValueError(f"Marker not found: {self.marker_id}")

        # Store old data for undo
        object.__setattr__(self, '_old_marker', old_marker)
        object.__setattr__(self, 'old_frame', old_marker.frame)

        # Create moved marker
        moved_marker = TimelineMarker(
            marker_id=old_marker.marker_id,
            frame=self.new_frame,
            name=old_marker.name,
            color=old_marker.color,
            comment=old_marker.comment,
            clip_id=old_marker.clip_id,
            track_id=old_marker.track_id,
        )

        # Update markers
        new_markers = tuple(
            moved_marker if m.marker_id == self.marker_id else m
            for m in timeline.markers
        )

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(
            f"MoveMarkerCommand applied: {self.marker_id} "
            f"from {self.old_frame} to {self.new_frame}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: move marker back to original position.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with marker at original position
        """
        if self._old_marker is None:
            raise ValueError("Cannot undo: old marker not stored")

        # Restore old marker
        new_markers = tuple(
            self._old_marker if m.marker_id == self.marker_id else m
            for m in timeline.markers
        )

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"MoveMarkerCommand undone: {self.marker_id} back to {self.old_frame}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveMarkerCommand",
            "label": self.label,
            "marker_id": self.marker_id,
            "new_frame": self.new_frame,
            "old_frame": self.old_frame,
            "_old_marker": self._old_marker.to_dict() if self._old_marker else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveMarkerCommand:
        """Deserialize command."""
        old_marker_data = data.get("_old_marker")
        old_marker = TimelineMarker.from_dict(old_marker_data) if old_marker_data else None

        return MoveMarkerCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Marker"),
            marker_id=data.get("marker_id", ""),
            new_frame=data.get("new_frame", 0),
            old_frame=data.get("old_frame"),
            _old_marker=old_marker,
        )

    @staticmethod
    def create(marker_id: str, new_frame: int) -> MoveMarkerCommand:
        """Factory method to create command."""
        return MoveMarkerCommand(
            command_id=str(uuid.uuid4()),
            label=f"Move Marker to frame {new_frame}",
            marker_id=marker_id,
            new_frame=new_frame,
        )


@dataclass(frozen=True)
class EditMarkerCommand:
    """
    Command to edit marker name and color.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        marker_id: Marker ID to edit
        new_name: New marker name (optional)
        new_color: New marker color hex string (optional)
        new_comment: New comment (optional)
        old_name: Old name (for undo)
        old_color: Old color (for undo)
        old_comment: Old comment (for undo)
        _old_marker: Internal: old marker data (for undo)
    """

    command_id: str
    label: str
    marker_id: str
    new_name: str | None = None
    new_color: str | None = None
    new_comment: str | None = None
    new_kind: str | None = None
    old_name: str | None = None
    old_color: str | None = None
    old_comment: str | None = None
    _old_marker: TimelineMarker | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: edit marker properties.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with edited marker
        """
        # Find marker
        old_marker = None
        for m in timeline.markers:
            if m.marker_id == self.marker_id:
                old_marker = m
                break

        if old_marker is None:
            raise ValueError(f"Marker not found: {self.marker_id}")

        # Store old data for undo
        object.__setattr__(self, '_old_marker', old_marker)
        object.__setattr__(self, 'old_name', old_marker.name)
        object.__setattr__(self, 'old_color', old_marker.color)
        object.__setattr__(self, 'old_comment', old_marker.comment)

        # Create edited marker
        edited_marker = TimelineMarker(
            marker_id=old_marker.marker_id,
            frame=old_marker.frame,
            name=self.new_name if self.new_name is not None else old_marker.name,
            color=self.new_color if self.new_color is not None else old_marker.color,
            comment=self.new_comment if self.new_comment is not None else old_marker.comment,
            clip_id=old_marker.clip_id,
            track_id=old_marker.track_id,
            kind=self.new_kind if self.new_kind is not None else old_marker.kind,
        )

        # Update markers
        new_markers = tuple(
            edited_marker if m.marker_id == self.marker_id else m
            for m in timeline.markers
        )

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"EditMarkerCommand applied: {self.marker_id}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original marker properties.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original marker
        """
        if self._old_marker is None:
            raise ValueError("Cannot undo: old marker not stored")

        # Restore old marker
        new_markers = tuple(
            self._old_marker if m.marker_id == self.marker_id else m
            for m in timeline.markers
        )

        new_timeline = timeline._rebuild(markers=new_markers)

        logger.debug(f"EditMarkerCommand undone: {self.marker_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "EditMarkerCommand",
            "label": self.label,
            "marker_id": self.marker_id,
            "new_name": self.new_name,
            "new_color": self.new_color,
            "new_comment": self.new_comment,
            "new_kind": self.new_kind,
            "old_name": self.old_name,
            "old_color": self.old_color,
            "old_comment": self.old_comment,
            "_old_marker": self._old_marker.to_dict() if self._old_marker else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EditMarkerCommand:
        """Deserialize command."""
        old_marker_data = data.get("_old_marker")
        old_marker = TimelineMarker.from_dict(old_marker_data) if old_marker_data else None

        return EditMarkerCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Edit Marker"),
            marker_id=data.get("marker_id", ""),
            new_name=data.get("new_name"),
            new_color=data.get("new_color"),
            new_comment=data.get("new_comment"),
            new_kind=data.get("new_kind"),
            old_name=data.get("old_name"),
            old_color=data.get("old_color"),
            old_comment=data.get("old_comment"),
            _old_marker=old_marker,
        )

    @staticmethod
    def create(
        marker_id: str,
        new_name: str | None = None,
        new_color: str | None = None,
        new_comment: str | None = None,
        new_kind: str | None = None,
    ) -> EditMarkerCommand:
        """Factory method to create command."""
        label_parts = []
        if new_name:
            label_parts.append(f"name='{new_name}'")
        if new_color:
            label_parts.append(f"color={new_color}")
        label = f"Edit Marker ({', '.join(label_parts)})" if label_parts else "Edit Marker"

        return EditMarkerCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            marker_id=marker_id,
            new_name=new_name,
            new_color=new_color,
            new_comment=new_comment,
            new_kind=new_kind,
        )
