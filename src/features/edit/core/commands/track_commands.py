"""Track management commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: track cluster (track mute/solo/lock/
sync-lock/visibility/color attributes, add/delete/rename/move track).
Class sources are byte-identical to the original module; to_dict/from_dict
keys are a persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple
import logging
import uuid

from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

logger = logging.getLogger(__name__)


# ================================================================
# Track Commands - Track Management Operations
# ================================================================


@dataclass(frozen=True)
class SetTrackMuteCommand:
    """
    Command to set track mute state.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to mute/unmute
        mute: New mute state
        _old_mute: Old mute state (for undo, set during apply)
    """

    command_id: str
    label: str
    track_id: str
    mute: bool
    _old_mute: bool | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set mute state."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_mute = track.is_muted

        new_track = track.with_muted(self.mute)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackMuteCommand applied: track {self.track_id} mute={self.mute}")

        # Return updated command with stored old_mute for undo
        updated_cmd = SetTrackMuteCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            mute=self.mute,
            _old_mute=old_mute,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old mute state."""
        if self._old_mute is None:
            logger.warning("SetTrackMuteCommand undo: old_mute not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_muted(self._old_mute)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackMuteCommand undone: track {self.track_id} mute={self._old_mute}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackMuteCommand",
            "label": self.label,
            "track_id": self.track_id,
            "mute": self.mute,
            "old_mute": self._old_mute,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackMuteCommand:
        return SetTrackMuteCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Mute"),
            track_id=data.get("track_id", ""),
            mute=data.get("mute", False),
            _old_mute=data.get("old_mute"),
        )

    @staticmethod
    def create(track_id: str, mute: bool) -> SetTrackMuteCommand:
        return SetTrackMuteCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Mute: {mute}",
            track_id=track_id,
            mute=mute,
        )


@dataclass(frozen=True)
class SetTrackSoloCommand:
    """
    Command to set track solo state.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to solo/unsolo
        solo: New solo state
        _old_solo: Old solo state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    solo: bool
    _old_solo: bool | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set solo state."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_solo = track.is_solo

        new_track = track.with_solo(self.solo)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackSoloCommand applied: track {self.track_id} solo={self.solo}")

        # Return updated command with stored old_solo for undo
        updated_cmd = SetTrackSoloCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            solo=self.solo,
            _old_solo=old_solo,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old solo state."""
        if self._old_solo is None:
            logger.warning("SetTrackSoloCommand undo: old_solo not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_solo(self._old_solo)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackSoloCommand undone: track {self.track_id} solo={self._old_solo}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackSoloCommand",
            "label": self.label,
            "track_id": self.track_id,
            "solo": self.solo,
            "old_solo": self._old_solo,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackSoloCommand:
        return SetTrackSoloCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Solo"),
            track_id=data.get("track_id", ""),
            solo=data.get("solo", False),
            _old_solo=data.get("old_solo"),
        )

    @staticmethod
    def create(track_id: str, solo: bool) -> SetTrackSoloCommand:
        return SetTrackSoloCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Solo: {solo}",
            track_id=track_id,
            solo=solo,
        )


@dataclass(frozen=True)
class SetTrackLockCommand:
    """
    Command to set track lock state.

    Locked tracks prevent editing of clips on that track.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to lock/unlock
        lock: New lock state
        _old_lock: Old lock state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    lock: bool
    _old_lock: bool | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set lock state."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_lock = track.is_locked

        new_track = track._rebuild(is_locked=self.lock)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackLockCommand applied: track {self.track_id} lock={self.lock}")

        # Return updated command with stored old_lock for undo
        updated_cmd = SetTrackLockCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            lock=self.lock,
            _old_lock=old_lock,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old lock state."""
        if self._old_lock is None:
            logger.warning("SetTrackLockCommand undo: old_lock not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track._rebuild(is_locked=self._old_lock)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackLockCommand undone: track {self.track_id} lock={self._old_lock}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackLockCommand",
            "label": self.label,
            "track_id": self.track_id,
            "lock": self.lock,
            "old_lock": self._old_lock,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackLockCommand:
        return SetTrackLockCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Lock"),
            track_id=data.get("track_id", ""),
            lock=data.get("lock", False),
            _old_lock=data.get("old_lock"),
        )

    @staticmethod
    def create(track_id: str, lock: bool) -> SetTrackLockCommand:
        return SetTrackLockCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Lock: {lock}",
            track_id=track_id,
            lock=lock,
        )


@dataclass(frozen=True)
class SetTrackSyncLockCommand:
    """
    Command to set track sync lock state (P1-05).

    Sync-locked tracks follow ripple/extract shifts that originate on the
    operated tracks; tracks with sync lock OFF keep their clip positions
    (deliberate desync, e.g. music/reference tracks). Orthogonal to the
    edit lock (``SetTrackLockCommand``): a sync-unlocked track can still be
    edited normally — it just ignores ripple shifts.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID
        sync_lock: New sync lock state
        _old_sync_lock: Old state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    sync_lock: bool
    _old_sync_lock: bool | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set sync lock state."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_sync_lock = track.is_sync_locked

        new_track = track.with_sync_locked(self.sync_lock)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(
            "SetTrackSyncLockCommand applied: track %s sync_lock=%s",
            self.track_id, self.sync_lock,
        )
        updated_cmd = SetTrackSyncLockCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            sync_lock=self.sync_lock,
            _old_sync_lock=old_sync_lock,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old sync lock state."""
        if self._old_sync_lock is None:
            logger.warning("SetTrackSyncLockCommand undo: old state not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_sync_locked(self._old_sync_lock)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(
            "SetTrackSyncLockCommand undone: track %s sync_lock=%s",
            self.track_id, self._old_sync_lock,
        )
        return new_timeline

    def redo(self, timeline: TimelineSequence) -> TimelineSequence:
        return self.apply(timeline)[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackSyncLockCommand",
            "label": self.label,
            "track_id": self.track_id,
            "sync_lock": self.sync_lock,
            "old_sync_lock": self._old_sync_lock,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackSyncLockCommand:
        return SetTrackSyncLockCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Sync Lock"),
            track_id=data.get("track_id", ""),
            sync_lock=data.get("sync_lock", True),
            _old_sync_lock=data.get("old_sync_lock"),
        )

    @staticmethod
    def create(track_id: str, sync_lock: bool) -> SetTrackSyncLockCommand:
        return SetTrackSyncLockCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Sync Lock: {sync_lock}",
            track_id=track_id,
            sync_lock=sync_lock,
        )


@dataclass(frozen=True)
class SetTrackVisibilityCommand:
    """
    Command to toggle track visibility.

    Invisible tracks are hidden in the timeline view but retain all clips.
    They are excluded from rendering and playback.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to toggle visibility
        visible: New visibility state
        _old_visible: Old visibility state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    visible: bool
    _old_visible: bool | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: set visibility."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_visible = track.is_visible
        new_track = track.with_visibility(self.visible)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackVisibilityCommand applied: track {self.track_id} visible={self.visible}")

        # Return updated command with stored old_visible for undo
        updated_cmd = SetTrackVisibilityCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            visible=self.visible,
            _old_visible=old_visible,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old visibility."""
        if self._old_visible is None:
            logger.warning("SetTrackVisibilityCommand undo: old_visible not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_visibility(self._old_visible)
        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"SetTrackVisibilityCommand undone: track {self.track_id} visible={self._old_visible}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackVisibilityCommand",
            "label": self.label,
            "track_id": self.track_id,
            "visible": self.visible,
            "old_visible": self._old_visible,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetTrackVisibilityCommand:
        return SetTrackVisibilityCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Visibility"),
            track_id=data.get("track_id", ""),
            visible=data.get("visible", True),
            _old_visible=data.get("old_visible"),
        )

    @staticmethod
    def create(track_id: str, visible: bool) -> SetTrackVisibilityCommand:
        return SetTrackVisibilityCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Track Visibility: {'Visible' if visible else 'Hidden'}",
            track_id=track_id,
            visible=visible,
        )


@dataclass(frozen=True)
class AddTrackCommand:
    """
    Command to add a new track to timeline.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_type: 'video' or 'audio'
        name: Track name (optional, auto-generated if empty)
        position: Position index to insert (optional, appended if None)
        track_id: Optional deterministic ID. Used by AI plans so preview and
            commit address the same track.
        _created_track_id: ID of created track (set during apply)
    """

    command_id: str
    label: str
    track_type: str
    name: str | None = None
    position: int | None = None
    track_id: str | None = None
    _created_track_id: str | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: add new track."""
        if self.track_type not in ("video", "audio", "subtitle"):
            raise ValueError(f"Invalid track_type: {self.track_type}")

        # Resolve existing track count and tuple by type
        if self.track_type == "video":
            existing = list(timeline.video_tracks)
        elif self.track_type == "audio":
            existing = list(timeline.audio_tracks)
        else:  # subtitle
            existing = list(timeline.subtitle_tracks)

        # ``ENSURE_*`` AI operations are intentionally idempotent. If the
        # deterministic target already exists, this command is a no-op and
        # must not remove that user-owned track during macro undo.
        if self.track_id is not None:
            existing_track = next(
                (track for track in existing if track.track_id == self.track_id),
                None,
            )
            if existing_track is not None:
                if existing_track.track_type != self.track_type:
                    raise ValueError(
                        f"Track {self.track_id} is not a {self.track_type} track"
                    )
                return timeline, AddTrackCommand(
                    command_id=self.command_id,
                    label=self.label,
                    track_type=self.track_type,
                    name=self.name,
                    position=self.position,
                    track_id=self.track_id,
                    _created_track_id=None,
                )
        existing_count = len(existing)

        position = self.position if self.position is not None else existing_count
        position = max(0, min(position, existing_count))

        if self.name:
            track_name = self.name
        else:
            if self.track_type == "video":
                track_name = f"V{position + 1}"
            elif self.track_type == "audio":
                track_name = f"A{position + 1}"
            else:  # subtitle
                track_name = f"S{position + 1}"

        track_id = self.track_id or str(uuid.uuid4())

        # Store created track_id for external access (bypass frozen dataclass)
        object.__setattr__(self, '_created_track_id', track_id)

        new_track = EditTrack(
            track_id=track_id,
            name=track_name,
            track_type=self.track_type,
            index=position,
            clips=(),
            subtitle_clips=(),
            transitions=(),
            is_enabled=True,
            is_locked=False,
            is_muted=False,
            is_solo=False,
            volume=1.0,
        )

        # Insert and re-index using EditTrack._rebuild to preserve all fields
        new_list = existing
        new_list.insert(position, new_track)
        new_tracks = tuple(t._rebuild(index=i) for i, t in enumerate(new_list))

        if self.track_type == "video":
            new_timeline = timeline._rebuild(video_tracks=new_tracks)
        elif self.track_type == "audio":
            new_timeline = timeline._rebuild(audio_tracks=new_tracks)
        else:  # subtitle
            new_timeline = timeline._rebuild(subtitle_tracks=new_tracks)

        logger.debug(f"AddTrackCommand applied: added {self.track_type} track {track_id} at position {position}")

        # Return updated command with stored track_id for undo
        updated_cmd = AddTrackCommand(
            command_id=self.command_id,
            label=self.label,
            track_type=self.track_type,
            name=self.name,
            position=position,
            track_id=self.track_id,
            _created_track_id=track_id,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: remove the added track."""
        # An explicit deterministic ID can represent an idempotent ensure
        # that found an existing track. In that case there is nothing to undo.
        if self.track_id is not None and self._created_track_id is None:
            return timeline
        if self._created_track_id:
            track_id = self._created_track_id
        else:
            if self.track_type == "video":
                tracks = timeline.video_tracks
            elif self.track_type == "audio":
                tracks = timeline.audio_tracks
            else:  # subtitle
                tracks = timeline.subtitle_tracks
            position = self.position if self.position is not None else len(tracks) - 1
            if position < len(tracks):
                track_id = tracks[position].track_id
            else:
                logger.warning("AddTrackCommand undo: cannot find track to remove")
                return timeline

        if self.track_type == "video":
            source = timeline.video_tracks
        elif self.track_type == "audio":
            source = timeline.audio_tracks
        else:  # subtitle
            source = timeline.subtitle_tracks

        new_tracks = tuple(
            t._rebuild(index=i)
            for i, t in enumerate(t for t in source if t.track_id != track_id)
        )

        if self.track_type == "video":
            new_timeline = timeline._rebuild(video_tracks=new_tracks)
        elif self.track_type == "audio":
            new_timeline = timeline._rebuild(audio_tracks=new_tracks)
        else:  # subtitle
            new_timeline = timeline._rebuild(subtitle_tracks=new_tracks)

        logger.debug(f"AddTrackCommand undone: removed track {track_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "AddTrackCommand",
            "label": self.label,
            "track_type": self.track_type,
            "name": self.name,
            "position": self.position,
            "track_id": self.track_id,
            "created_track_id": self._created_track_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddTrackCommand:
        return AddTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Track"),
            track_type=data.get("track_type", "video"),
            name=data.get("name"),
            position=data.get("position"),
            track_id=data.get("track_id"),
            _created_track_id=data.get("created_track_id"),
        )

    @staticmethod
    def create(
        track_type: str,
        name: str | None = None,
        position: int | None = None,
        track_id: str | None = None,
    ) -> AddTrackCommand:
        return AddTrackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add {track_type} Track",
            track_type=track_type,
            name=name,
            position=position,
            track_id=track_id,
        )


@dataclass(frozen=True)
class DeleteTrackCommand:
    """
    Command to delete a track from timeline.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to delete
        _deleted_track: Deleted track data (for undo)
        _track_type: Track type (for undo)
    """

    command_id: str
    label: str
    track_id: str
    _deleted_track: dict[str, Any] | None = None
    _track_type: str | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: delete track."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        deleted_track_data = track.to_dict()
        track_type = track.track_type

        # Pick the right source tuple by type
        if track_type == "video":
            source = timeline.video_tracks
        elif track_type == "audio":
            source = timeline.audio_tracks
        else:  # subtitle
            source = timeline.subtitle_tracks

        # Re-index remaining tracks, preserving ALL fields via _rebuild
        new_tracks = tuple(
            t._rebuild(index=i)
            for i, t in enumerate(t for t in source if t.track_id != self.track_id)
        )

        if track_type == "video":
            new_timeline = timeline._rebuild(video_tracks=new_tracks)
        elif track_type == "audio":
            new_timeline = timeline._rebuild(audio_tracks=new_tracks)
        else:  # subtitle
            new_timeline = timeline._rebuild(subtitle_tracks=new_tracks)

        logger.debug(f"DeleteTrackCommand applied: deleted track {self.track_id}")

        # Return updated command with stored data for undo
        updated_cmd = DeleteTrackCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            _deleted_track=deleted_track_data,
            _track_type=track_type,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore deleted track."""
        if self._deleted_track is None:
            logger.warning("DeleteTrackCommand undo: deleted_track not stored")
            return timeline

        restored_track = EditTrack.from_dict(self._deleted_track)
        track_type = self._track_type or restored_track.track_type

        if track_type == "video":
            source = list(timeline.video_tracks)
        elif track_type == "audio":
            source = list(timeline.audio_tracks)
        else:  # subtitle
            source = list(timeline.subtitle_tracks)

        # Insert restored track at its original index, then re-index all
        source.insert(restored_track.index, restored_track)
        new_tracks = tuple(t._rebuild(index=i) for i, t in enumerate(source))

        if track_type == "video":
            new_timeline = timeline._rebuild(video_tracks=new_tracks)
        elif track_type == "audio":
            new_timeline = timeline._rebuild(audio_tracks=new_tracks)
        else:  # subtitle
            new_timeline = timeline._rebuild(subtitle_tracks=new_tracks)

        logger.debug(f"DeleteTrackCommand undone: restored track {self.track_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "DeleteTrackCommand",
            "label": self.label,
            "track_id": self.track_id,
            "deleted_track": self._deleted_track,
            "track_type": self._track_type,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteTrackCommand:
        return DeleteTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Track"),
            track_id=data.get("track_id", ""),
            _deleted_track=data.get("deleted_track"),
            _track_type=data.get("track_type"),
        )

    @staticmethod
    def create(track_id: str) -> DeleteTrackCommand:
        return DeleteTrackCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Track",
            track_id=track_id,
        )


@dataclass(frozen=True)
class RenameTrackCommand:
    """
    Command to rename a track.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to rename
        new_name: New track name
        _old_name: Old track name (for undo)
    """

    command_id: str
    label: str
    track_id: str
    new_name: str
    _old_name: str | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: rename track."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        old_name = track.name
        # W16: 用 with_name 保留所有字段（含 strip_eq/color 等新字段）
        new_track = track.with_name(self.new_name)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"RenameTrackCommand applied: '{old_name}' -> '{self.new_name}'")

        # Return updated command with stored old_name for undo
        updated_cmd = RenameTrackCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            new_name=self.new_name,
            _old_name=old_name,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: restore old name."""
        if self._old_name is None:
            logger.warning("RenameTrackCommand undo: old_name not stored")
            return timeline

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        new_track = track.with_name(self._old_name)

        new_timeline = timeline.with_track_updated(new_track)
        logger.debug(f"RenameTrackCommand undone: '{self.new_name}' -> '{self._old_name}'")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "RenameTrackCommand",
            "label": self.label,
            "track_id": self.track_id,
            "new_name": self.new_name,
            "old_name": self._old_name,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RenameTrackCommand:
        return RenameTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Rename Track"),
            track_id=data.get("track_id", ""),
            new_name=data.get("new_name", ""),
            _old_name=data.get("old_name"),
        )

    @staticmethod
    def create(track_id: str, new_name: str) -> RenameTrackCommand:
        return RenameTrackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Rename Track to '{new_name}'",
            track_id=track_id,
            new_name=new_name,
        )


@dataclass(frozen=True)
class SetTrackColorCommand:
    """
    Command to set a track's color (W16).

    Color is a hex string (e.g. "#ff5722") or None to clear.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID
        color: New color (hex string | None)
        _old_color: Old color (for undo)
    """

    command_id: str
    label: str
    track_id: str
    color: Optional[str]
    _old_color: Optional[str] = None

    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetTrackColorCommand"]:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        old_color = track.color
        new_track = track.with_color(self.color)
        new_timeline = timeline.with_track_updated(new_track)
        updated_cmd = SetTrackColorCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, color=self.color, _old_color=old_color,
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        return timeline.with_track_updated(track.with_color(self._old_color))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetTrackColorCommand",
            "label": self.label,
            "track_id": self.track_id,
            "color": self.color,
            "old_color": self._old_color,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SetTrackColorCommand":
        return SetTrackColorCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Track Color"),
            track_id=data.get("track_id", ""),
            color=data.get("color"),
            _old_color=data.get("old_color"),
        )

    @staticmethod
    def create(track_id: str, color: Optional[str]) -> "SetTrackColorCommand":
        return SetTrackColorCommand(
            command_id=str(uuid.uuid4()),
            label="Set Track Color",
            track_id=track_id,
            color=color,
        )


@dataclass(frozen=True)
class MoveTrackCommand:
    """
    Command to move a track up or down.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track ID to move
        direction: 'up' or 'down'
        _old_index: Old index (for undo)
    """

    command_id: str
    label: str
    track_id: str
    direction: str  # 'up' or 'down'
    _old_index: int | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command: move track."""
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        track_type = track.track_type
        if track_type == "video":
            tracks_attr = "video_tracks"
        elif track_type == "audio":
            tracks_attr = "audio_tracks"
        else:
            tracks_attr = "subtitle_tracks"
        tracks = list(getattr(timeline, tracks_attr))
        old_index = next(
            (index for index, item in enumerate(tracks)
             if item.track_id == self.track_id),
            -1,
        )
        if old_index < 0:
            raise ValueError(f"Track not found in {tracks_attr}: {self.track_id}")

        if self.direction == "up":
            new_index = old_index - 1
        else:
            new_index = old_index + 1

        if new_index < 0 or new_index >= len(tracks):
            logger.warning(f"MoveTrackCommand: cannot move {self.direction}, out of bounds")
            return timeline

        tracks[old_index], tracks[new_index] = tracks[new_index], tracks[old_index]

        tracks = [t._rebuild(index=i) for i, t in enumerate(tracks)]
        new_timeline = timeline._rebuild(**{tracks_attr: tuple(tracks)})

        logger.debug(f"MoveTrackCommand applied: track {self.track_id} moved {self.direction}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command: move track back."""
        opposite = "down" if self.direction == "up" else "up"
        undo_cmd = MoveTrackCommand(
            command_id=self.command_id,
            label="Undo Move Track",
            track_id=self.track_id,
            direction=opposite,
            _old_index=None,
        )
        return undo_cmd.apply(timeline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "MoveTrackCommand",
            "label": self.label,
            "track_id": self.track_id,
            "direction": self.direction,
            "old_index": self._old_index,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MoveTrackCommand:
        return MoveTrackCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Track"),
            track_id=data.get("track_id", ""),
            direction=data.get("direction", "up"),
            _old_index=data.get("old_index"),
        )

    @staticmethod
    def create(track_id: str, direction: str) -> MoveTrackCommand:
        return MoveTrackCommand(
            command_id=str(uuid.uuid4()),
            label=f"Move Track {direction}",
            track_id=track_id,
            direction=direction,
        )
