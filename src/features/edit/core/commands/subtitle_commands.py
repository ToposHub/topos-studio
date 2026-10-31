"""Subtitle clip commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: subtitle cluster (caption cue add/
update/move/copy/delete + CaptionEditMode) together with the subtitle-only
review-lock helpers. Class sources are byte-identical to the original
module; to_dict/from_dict keys are a persistence contract — do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional
import logging
import uuid

from src.features.edit.core.timeline_v2 import ClipInstance, SubtitleClip, TimelineSequence
from src.features.edit.core.captions.model import CaptionCueMetadata, CaptionWordTiming

logger = logging.getLogger(__name__)


def _is_pure_caption_unlock(clip: SubtitleClip, updates: dict[str, Any]) -> bool:
    """Return True only for a metadata-only transition out of a cue lock.

    Locked cues must still have an escape hatch.  The unlock command may
    change only ``locked``/``status``; combining an unlock with a text, timing,
    style, assignee, or note edit remains forbidden.
    """
    if set(updates) != {"caption_metadata"}:
        return False
    new_metadata = updates.get("caption_metadata")
    if new_metadata is None or not hasattr(new_metadata, "is_edit_locked"):
        return False
    if new_metadata.is_edit_locked():
        return False
    old_data = clip.caption_metadata.to_dict()
    new_data = new_metadata.to_dict()
    ignored = {"locked", "status"}
    keys = (set(old_data) | set(new_data)) - ignored
    return all(old_data.get(key) == new_data.get(key) for key in keys)


def _ensure_subtitle_clip_editable(
    timeline: TimelineSequence,
    clip: SubtitleClip,
    updates: Optional[dict[str, Any]] = None,
) -> None:
    """Reject mutations of a review-locked cue or locked owner track."""
    owner = timeline.find_subtitle_clip_owner_track(clip.clip_id)
    if owner is not None and owner.is_locked:
        raise ValueError(
            f"Subtitle track is locked and cannot be edited: {owner.track_id}"
        )
    if clip.is_edit_locked() and not _is_pure_caption_unlock(clip, updates or {}):
        raise ValueError(f"Subtitle clip is locked and cannot be edited: {clip.clip_id}")


# ================================================================
# P10.3 - Subtitle Clip Commands
# ================================================================


class CaptionEditMode:
    """Placement strategy when creating a new subtitle cue (SUB-P1-07).

    Mirrors the professional-NLE insert/overwrite/ripple distinction so a new
    cue lands predictably instead of always being silently bumped past the
    next clip. Constants (not enum) match the project's EditTool convention.
    """
    OVERWRITE = "overwrite"  # default: split/trim clips overlapped by the new cue
    INSERT = "insert"        # place after the first overlapping clip (legacy default)
    RIPPLE = "ripple"        # shift every clip at/after the start later by duration


@dataclass(frozen=True)
class AddSubtitleClipCommand:
    """Command to add a new subtitle clip to a subtitle track.

    The default duration is read from the application config
    (default_subtitle_clip_duration_frames) when not specified explicitly.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Target subtitle track ID
        timeline_start: Start frame on the timeline
        duration: Clip duration in frames (optional, falls back to config default)
        text: Initial subtitle text (optional)
        _created_clip_id: ID of created clip (set during apply)
    """
    command_id: str
    label: str
    track_id: str
    timeline_start: int
    duration: int | None = None
    text: str = ""
    # SUB-P1-07: placement strategy (see CaptionEditMode). Default overwrite.
    mode: str = CaptionEditMode.OVERWRITE
    word_timings: tuple[CaptionWordTiming, ...] = ()
    # AI plans provide this so preview and commit produce the same cue IDs.
    clip_id: str | None = None
    _created_clip_id: str | None = None
    # SUB-P1-07: snapshots of clips displaced by overwrite/ripple, for undo.
    # Stored as a list of (clip_id, original_clip_dict) pairs.
    _displaced_clips: tuple = ()
    # SUB-P2-01: optional full-clip template. When set (via from_clip), apply()
    # stamps the new cue from this template via SubtitleClip._replace so the
    # split/merge halves carry style_override + caption_metadata from the
    # original cue, not just text/timing. None → legacy minimal constructor.
    _template_clip: object = None

    def __post_init__(self) -> None:
        valid_modes = {
            CaptionEditMode.INSERT,
            CaptionEditMode.OVERWRITE,
            CaptionEditMode.RIPPLE,
        }
        if self.mode not in valid_modes:
            raise ValueError(f"Invalid caption edit mode: {self.mode!r}")

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply: create a SubtitleClip and insert into the target track.

        SUB-P1-07: placement follows ``self.mode`` (CaptionEditMode). Returns
        ``(new_timeline, updated_cmd)`` so the caller can keep the resolved
        start/duration and the displaced-clip snapshot for undo.
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Subtitle track not found: {self.track_id}")
        if track.track_type != "subtitle":
            raise ValueError(f"Track {self.track_id} is not a subtitle track (got {track.track_type})")
        if track.is_locked:
            raise ValueError(
                f"Subtitle track is locked and cannot be edited: {self.track_id}"
            )

        # Resolve duration: explicit > config default > 72 frames (3s @ 24fps)
        duration = self.duration
        if duration is None:
            try:
                from src.shared.config.config_manager import ConfigManager
                duration = int(ConfigManager.DEFAULT_CONFIG.get(
                    "default_subtitle_clip_duration_frames", 72))
            except Exception:
                duration = 72
        duration = max(1, duration)

        start = max(0, self.timeline_start)
        displaced: list[tuple[str, dict]] = []

        # Build the new cue once so its generated id is captured directly.
        # SUB-P2-01: when a template clip is supplied (from_clip), stamp from
        # it so style_override + caption_metadata carry to split/merge halves.
        # A fresh clip_id is generated so the new cue has its own identity.
        if self._template_clip is not None:
            metadata = self._template_clip.caption_metadata
            if self.word_timings:
                metadata = metadata.with_word_timings(
                    self.word_timings, animation="karaoke"
                )
            new_clip = self._template_clip._replace(
                clip_id=str(uuid.uuid4()),
                track_id=self.track_id,
                timeline_start=start,
                timeline_duration=duration,
                text=self.text,
                caption_metadata=metadata,
            )
        else:
            metadata = CaptionCueMetadata()
            if self.word_timings:
                metadata = metadata.with_word_timings(
                    self.word_timings, animation="karaoke"
                )
            new_clip = SubtitleClip(
                clip_id=self.clip_id or str(uuid.uuid4()),
                track_id=self.track_id, timeline_start=start,
                timeline_duration=duration, text=self.text,
                caption_metadata=metadata,
            )

        # OVERWRITE/RIPPLE mutate existing cues as a side effect.  A review-
        # locked cue cannot be deleted, trimmed, or shifted indirectly by an
        # add command.  INSERT is safe because it only advances the new cue
        # past occupied spans and leaves existing cues untouched.
        affected_locked: list[str] = []
        if self.mode == CaptionEditMode.RIPPLE:
            affected_locked = [
                clip.clip_id for clip in track.subtitle_clips
                if (clip.timeline_start >= start or clip.timeline_end > start)
                and clip.is_edit_locked()
            ]
        elif self.mode == CaptionEditMode.OVERWRITE:
            new_end = start + duration
            affected_locked = [
                clip.clip_id for clip in track.subtitle_clips
                if clip.timeline_start < new_end and clip.timeline_end > start
                and clip.is_edit_locked()
            ]
        if affected_locked:
            raise ValueError(
                "Locked subtitle clips cannot be displaced: "
                + ", ".join(affected_locked)
            )

        if self.mode == CaptionEditMode.INSERT:
            # Legacy default: advance past any overlapping clip.
            start = self._resolve_insert_start(track, start, duration)
            new_clip = new_clip.with_params(timeline_start=start)
            new_timeline = timeline.with_subtitle_clip_added(self.track_id, new_clip)
        elif self.mode == CaptionEditMode.RIPPLE:
            new_track, displaced = self._apply_ripple(track, start, duration)
            new_timeline = timeline.with_track_updated(new_track)
            new_timeline = new_timeline.with_subtitle_clip_added(self.track_id, new_clip)
        else:  # OVERWRITE (default)
            new_track, displaced = self._apply_overwrite(track, start, duration)
            new_timeline = timeline.with_track_updated(new_track)
            new_timeline = new_timeline.with_subtitle_clip_added(self.track_id, new_clip)

        object.__setattr__(self, '_created_clip_id', new_clip.clip_id)
        object.__setattr__(self, '_displaced_clips', tuple(displaced))

        logger.debug(
            f"AddSubtitleClipCommand applied ({self.mode}): clip {new_clip.clip_id} "
            f"@ frame {start} dur {duration}"
            + (f" displaced={len(displaced)}" if displaced else "")
        )

        updated_cmd = AddSubtitleClipCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            timeline_start=start,
            duration=duration,
            text=self.text,
            mode=self.mode,
            word_timings=self.word_timings,
            clip_id=self.clip_id,
            _created_clip_id=new_clip.clip_id,
            _displaced_clips=tuple(displaced),
            _template_clip=self._template_clip,
        )
        return new_timeline, updated_cmd

    @staticmethod
    def _resolve_insert_start(track, start: int, duration: int) -> int:
        """INSERT: place at ``start`` or, if it overlaps, after the overlap."""
        existing = sorted(track.subtitle_clips, key=lambda c: c.timeline_start)
        for _ in range(len(existing) + 1):
            overlap = None
            for c in existing:
                if start < c.timeline_end and start + duration > c.timeline_start:
                    overlap = c
                    break
            if overlap is None:
                return start
            start = max(start, overlap.timeline_end)
        return start

    @staticmethod
    def _apply_overwrite(track, start: int, duration: int):
        """OVERWRITE: snapshot + trim/remove clips overlapped by [start, start+duration).

        Returns (new_track, displaced_snapshots). For a clip fully inside the
        new span it is removed; for a left/right partial overlap it is trimmed.
        Middle-of-clip overlap (new cue splits an existing cue in two) is not
        split here — the whole clip is removed (TODO: split). Subtitle cues are
        short so this is rare.
        """
        new_end = start + duration
        displaced: list[tuple[str, dict]] = []
        new_clips: list = []
        for c in track.subtitle_clips:
            overlap = c.timeline_start < new_end and c.timeline_end > start
            if not overlap:
                new_clips.append(c)
                continue
            # Snapshot the original for undo.
            displaced.append((c.clip_id, c.to_dict()))
            if start <= c.timeline_start and c.timeline_end <= new_end:
                # Fully covered → drop.
                continue
            if c.timeline_start < start and c.timeline_end > new_end:
                # Middle overlap (cue wraps around both ends) → drop whole cue.
                # TODO: split into two cues instead.
                continue
            if c.timeline_start < start:
                # Right part of the existing cue is covered → trim its duration
                # so its end becomes the new cue's start (end stays where the
                # covered region began).
                new_dur = start - c.timeline_start
                if new_dur >= 1:
                    new_clips.append(c.with_params(timeline_duration=new_dur))
            else:
                # c.timeline_start >= start and c.timeline_end > new_end:
                # Left part covered → advance start to new_end, keep end fixed
                # (shrink duration by the covered amount).
                covered = new_end - c.timeline_start
                new_clips.append(c.with_params(
                    timeline_start=new_end,
                    timeline_duration=max(1, c.timeline_duration - covered),
                ))
        new_clips_sorted = tuple(sorted(new_clips, key=lambda c: c.timeline_start))
        return track._rebuild(subtitle_clips=new_clips_sorted), displaced

    def _apply_ripple(self, track, start: int, duration: int):
        """RIPPLE: open a gap [start, start+duration) and shift later material.

        - Clips starting at/after ``start`` shift later by ``duration``.
        - A clip straddling ``start`` (start < insertion < end) is trimmed to
          end at ``start`` so the gap is clean (its tail would otherwise still
          cover the insertion point).
        Returns (new_track, displaced_snapshots).
        """
        displaced: list[tuple[str, dict]] = []
        new_clips: list = []
        for c in track.subtitle_clips:
            if c.timeline_start >= start:
                displaced.append((c.clip_id, c.to_dict()))
                new_clips.append(c.with_params(timeline_start=c.timeline_start + duration))
            elif c.timeline_end > start:
                # Straddles the insertion point → trim to end at start.
                displaced.append((c.clip_id, c.to_dict()))
                new_dur = start - c.timeline_start
                if new_dur >= 1:
                    new_clips.append(c.with_params(timeline_duration=new_dur))
            else:
                new_clips.append(c)
        new_clips_sorted = tuple(sorted(new_clips, key=lambda c: c.timeline_start))
        return track._rebuild(subtitle_clips=new_clips_sorted), displaced

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo: remove the created cue and restore any displaced clips."""
        clip_id = self._created_clip_id
        if clip_id is None:
            logger.warning("AddSubtitleClipCommand.undo: no clip_id stored")
            return timeline
        new_timeline = timeline.with_subtitle_clip_removed(clip_id)
        # SUB-P1-07: restore clips that overwrite/ripple displaced. Each
        # displaced clip's current (modified) version must be removed first,
        # because its trimmed/shifted span may overlap the original we restore.
        if self._displaced_clips:
            for displaced_id, clip_dict in self._displaced_clips:
                if new_timeline.get_subtitle_clip_by_id(displaced_id) is not None:
                    new_timeline = new_timeline.with_subtitle_clip_removed(displaced_id)
            for _displaced_id, clip_dict in self._displaced_clips:
                restored = SubtitleClip.from_dict(clip_dict)
                new_timeline = new_timeline.with_subtitle_clip_added(
                    restored.track_id, restored)
        logger.debug(
            f"AddSubtitleClipCommand undone: removed clip {clip_id}"
            + (f" restored {len(self._displaced_clips)} displaced" if self._displaced_clips else "")
        )
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "AddSubtitleClipCommand",
            "label": self.label,
            "track_id": self.track_id,
            "timeline_start": self.timeline_start,
            "duration": self.duration,
            "text": self.text,
            "mode": self.mode,
            "word_timings": [word.to_dict() for word in self.word_timings],
            "clip_id": self.clip_id,
            "created_clip_id": self._created_clip_id,
            "displaced_clips": list(self._displaced_clips),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddSubtitleClipCommand:
        return AddSubtitleClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Subtitle Clip"),
            track_id=data["track_id"],
            timeline_start=data["timeline_start"],
            duration=data.get("duration"),
            text=data.get("text", ""),
            mode=data.get("mode", CaptionEditMode.INSERT),
            word_timings=tuple(
                CaptionWordTiming.from_dict(item)
                for item in (data.get("word_timings") or ())
                if isinstance(item, dict)
            ),
            clip_id=data.get("clip_id"),
            _created_clip_id=data.get("created_clip_id"),
            _displaced_clips=tuple(data.get("displaced_clips") or ()),
        )

    @staticmethod
    def create(track_id: str, timeline_start: int,
               duration: int | None = None, text: str = "",
               mode: str = CaptionEditMode.OVERWRITE,
               clip_id: str | None = None,
               word_timings: tuple[CaptionWordTiming, ...] = ()) -> AddSubtitleClipCommand:
        return AddSubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label="Add Subtitle Clip",
            track_id=track_id,
            timeline_start=timeline_start,
            duration=duration,
            text=text,
            mode=mode,
            clip_id=clip_id,
            word_timings=tuple(word_timings),
        )

    @classmethod
    def from_clip(cls, track_id: str, template, *,
                  timeline_start: int, duration: int, text: str,
                  mode: str = CaptionEditMode.OVERWRITE,
                  label: str = "Add Subtitle Clip") -> "AddSubtitleClipCommand":
        """Create an add command that stamps the new cue from a ``template`` clip.

        SUB-P2-01: unlike :meth:`create`, the new cue inherits every field of
        ``template`` (notably ``style_override`` and ``caption_metadata``) via
        ``SubtitleClip._replace``; only ``clip_id`` (regenerated),
        ``track_id``, ``timeline_start``, ``timeline_duration`` and ``text`` are
        overridden. Used by Split/Merge so the halves keep the original cue's
        caption style and review metadata.
        """
        return cls(
            command_id=str(uuid.uuid4()),
            label=label,
            track_id=track_id,
            timeline_start=timeline_start,
            duration=duration,
            text=text,
            mode=mode,
            _template_clip=template,
        )


@dataclass(frozen=True)
class UpdateSubtitleClipCommand:
    """Command to update one or more fields of an existing subtitle clip.

    Only the fields present in `updates` are modified; all others are
    preserved from the current clip state. Used by:
    - Properties panel (text/font/color/transform edits)
    - Handle controller drag (translate/scale/rotation, merged per drag)

    SUB-P0-03 patch whitelist: identity fields (``clip_id``/``track_id``) are
    forbidden here. Renaming an id or moving a cue to another track would
    orphan it from its owner track invariant (EditTrack validates
    ``sub.track_id == track.track_id``) and must go through a dedicated move
    command. Timing/style/transform/state fields are all permitted.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        clip_id: Target subtitle clip ID
        updates: Dict of field → new value (subset of SubtitleClip fields)
        _previous: Snapshot of previous values for undo (set during apply)
    """
    command_id: str
    label: str
    clip_id: str
    updates: dict[str, Any] = field(default_factory=dict)
    _previous: dict[str, Any] | None = None

    # SUB-P0-03: fields Update may never touch. clip_id is the lookup key;
    # track_id is enforced by EditTrack (sub.track_id == owner.track_id) so
    # changing it here would build an orphaned clip. Use a dedicated move
    # command for cross-track moves once introduced. Declared as a ClassVar
    # so it is NOT treated as a dataclass field.
    FORBIDDEN_UPDATE_FIELDS: ClassVar[tuple[str, ...]] = ("clip_id", "track_id")

    def __post_init__(self) -> None:
        forbidden = set(self.FORBIDDEN_UPDATE_FIELDS) & set(self.updates)
        if forbidden:
            raise ValueError(
                f"UpdateSubtitleClipCommand cannot modify identity fields: "
                f"{sorted(forbidden)}; use a dedicated move/rename command."
            )

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_subtitle_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Subtitle clip not found: {self.clip_id}")
        _ensure_subtitle_clip_editable(timeline, clip, self.updates)

        # Snapshot previous values for undo (only the fields being changed).
        # SUB-P0-02: when an explicit previous snapshot is supplied (e.g. from
        # a SubtitleTransformSession that captured the pre-drag state), honour
        # it instead of re-deriving from the (possibly already-mutated) clip.
        if self._previous is not None:
            prev = {k: v for k, v in self._previous.items() if hasattr(clip, k)}
        else:
            prev: dict[str, Any] = {}
            for key in self.updates:
                if hasattr(clip, key):
                    prev[key] = getattr(clip, key)
        object.__setattr__(self, '_previous', prev)

        new_clip = clip.with_params(**self.updates)
        new_timeline = timeline.with_subtitle_clip_updated(new_clip)
        # Binding synchronization is part of the same immutable command. The
        # source cue remains authoritative; the graphic clip only mirrors its
        # timing when the binding opted into timing sync.
        if any(key in self.updates for key in ("timeline_start", "timeline_duration")):
            new_timeline = new_timeline.sync_caption_graphic_for_cue(self.clip_id)
        logger.debug(
            f"UpdateSubtitleClipCommand applied: clip {self.clip_id} "
            f"fields={list(self.updates.keys())}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if not self._previous:
            logger.warning("UpdateSubtitleClipCommand.undo: no previous snapshot")
            return timeline
        clip = timeline.get_subtitle_clip_by_id(self.clip_id)
        if clip is None:
            logger.warning(f"UpdateSubtitleClipCommand.undo: clip {self.clip_id} gone")
            return timeline
        new_clip = clip.with_params(**self._previous)
        new_timeline = timeline.with_subtitle_clip_updated(new_clip)
        # AUD-16207: apply 在时序字段变更时同步过 caption graphic，undo
        # 必须对称再同步一次——否则 cue 时间还原而绑定图形仍停在 apply
        # 后的位置，图文错位在 undo 后残留。
        if any(
            key in ("timeline_start", "timeline_duration")
            for key in (*self.updates.keys(), *self._previous.keys())
        ):
            new_timeline = new_timeline.sync_caption_graphic_for_cue(self.clip_id)
        logger.debug(f"UpdateSubtitleClipCommand undone: clip {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "UpdateSubtitleClipCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "updates": self.updates,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateSubtitleClipCommand:
        return UpdateSubtitleClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Subtitle Clip"),
            clip_id=data["clip_id"],
            updates=data.get("updates", {}),
        )

    @staticmethod
    def create(clip_id: str, updates: dict[str, Any],
               label: str = "Update Subtitle Clip") -> UpdateSubtitleClipCommand:
        return UpdateSubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            clip_id=clip_id,
            updates=dict(updates),
        )

    @staticmethod
    def create_with_previous(
        clip_id: str,
        updates: dict[str, Any],
        previous: dict[str, Any],
        label: str = "Update Subtitle Clip",
    ) -> UpdateSubtitleClipCommand:
        """Build a command with an explicit pre-edit snapshot.

        SUB-P0-02: transform-handle drags mutate the timeline live during the
        drag (for real-time preview) and only commit once on pointer-up. A
        command built at release time must carry the *pre-drag* values as its
        undo snapshot, otherwise undo restores the already-final state and is
        a no-op. ``previous`` is the immutable snapshot captured at drag start.
        """
        return UpdateSubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            clip_id=clip_id,
            updates=dict(updates),
            _previous=dict(previous),
        )


@dataclass(frozen=True)
class MoveSubtitleClipCommand:
    """Move one subtitle cue in time and, optionally, to another subtitle track.

    ``UpdateSubtitleClipCommand`` intentionally rejects ``track_id`` changes
    because owner-track changes require removing the cue from one immutable
    track and inserting it into another while preserving sequence uniqueness.
    This command provides that atomic, undoable operation.
    """

    command_id: str
    label: str
    clip_id: str
    target_track_id: str
    timeline_start: int
    # Timeline drags opt into overwrite mode. The strict default remains for
    # other callers that expect overlap validation to fail.
    overwrite_overlaps: bool = False
    _source_track_id: str | None = None
    _previous_start: int | None = None
    _displaced_clips: tuple = ()

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_subtitle_clip_by_id(self.clip_id)
        source_track = timeline.find_subtitle_clip_owner_track(self.clip_id)
        target_track = timeline.get_track_by_id(self.target_track_id)
        if clip is None or source_track is None:
            raise ValueError(f"Subtitle clip not found: {self.clip_id}")
        _ensure_subtitle_clip_editable(timeline, clip)
        if target_track is None or target_track.track_type != "subtitle":
            raise ValueError(f"Target is not a subtitle track: {self.target_track_id}")
        if target_track.is_locked:
            raise ValueError(
                f"Subtitle track is locked and cannot be edited: {target_track.track_id}"
            )

        start = max(0, int(self.timeline_start))
        moved = clip._replace(track_id=target_track.track_id, timeline_start=start)
        displaced: list[tuple[str, dict]] = []
        if self.overwrite_overlaps:
            # For a same-track move, remove the source before deciding which
            # *other* cues are covered by the selected cue's new span.
            base_target = target_track
            if source_track.track_id == target_track.track_id:
                base_target = target_track.with_subtitle_clip_removed(self.clip_id)
            affected_locked = [
                existing.clip_id for existing in base_target.subtitle_clips
                if existing.timeline_start < moved.timeline_end
                and existing.timeline_end > moved.timeline_start
                and existing.is_edit_locked()
            ]
            if affected_locked:
                raise ValueError(
                    "Locked subtitle clips cannot be displaced: "
                    + ", ".join(affected_locked)
                )
            # Reuse AddSubtitleClipCommand's established semantics: fully
            # covered cues are removed; edge overlaps are trimmed.
            target_updated, displaced = AddSubtitleClipCommand._apply_overwrite(
                base_target, start, moved.timeline_duration,
            )
            target_updated = target_updated.with_subtitle_clip_added(moved)
            if source_track.track_id == target_track.track_id:
                updated = timeline.with_track_updated(target_updated)
            else:
                source_updated = source_track.with_subtitle_clip_removed(self.clip_id)
                updated = timeline.with_track_updated(source_updated)
                updated = updated.with_track_updated(target_updated)
        elif source_track.track_id == target_track.track_id:
            updated = timeline.with_subtitle_clip_updated(moved)
        else:
            # Validate the destination before removing from the source, so an
            # overlap failure leaves the original immutable timeline untouched.
            target_updated = target_track.with_subtitle_clip_added(moved)
            source_updated = source_track.with_subtitle_clip_removed(self.clip_id)
            updated = timeline.with_track_updated(source_updated)
            updated = updated.with_track_updated(target_updated)

        object.__setattr__(self, "_source_track_id", source_track.track_id)
        object.__setattr__(self, "_previous_start", int(clip.timeline_start))
        object.__setattr__(self, "_displaced_clips", tuple(displaced))
        return updated

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._source_track_id is None or self._previous_start is None:
            logger.warning("MoveSubtitleClipCommand.undo: no previous snapshot")
            return timeline
        clip = timeline.get_subtitle_clip_by_id(self.clip_id)
        if clip is None:
            logger.warning("MoveSubtitleClipCommand.undo: clip is missing")
            return timeline
        restored = clip._replace(
            track_id=self._source_track_id,
            timeline_start=self._previous_start,
        )
        current_track = timeline.find_subtitle_clip_owner_track(self.clip_id)
        if current_track is None:
            return timeline
        # Remove the moved cue first, then replace every trimmed/remnant cue
        # with the precise snapshot captured before the overwrite.
        updated = timeline.with_subtitle_clip_removed(self.clip_id)
        for displaced_id, _clip_dict in self._displaced_clips:
            if updated.get_subtitle_clip_by_id(displaced_id) is not None:
                updated = updated.with_subtitle_clip_removed(displaced_id)
        for _displaced_id, clip_dict in self._displaced_clips:
            original = SubtitleClip.from_dict(clip_dict)
            updated = updated.with_subtitle_clip_added(original.track_id, original)
        return updated.with_subtitle_clip_added(self._source_track_id, restored)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the deterministic move parameters and undo snapshot."""
        return {
            "command_id": self.command_id,
            "command_type": "MoveSubtitleClipCommand",
            "label": self.label,
            "clip_id": self.clip_id,
            "target_track_id": self.target_track_id,
            "timeline_start": self.timeline_start,
            "overwrite_overlaps": self.overwrite_overlaps,
            "source_track_id": self._source_track_id,
            "previous_start": self._previous_start,
            "displaced_clips": list(self._displaced_clips),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MoveSubtitleClipCommand":
        """Deserialize a subtitle move command."""
        return MoveSubtitleClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Subtitle Clip"),
            clip_id=data["clip_id"],
            target_track_id=data["target_track_id"],
            timeline_start=int(data["timeline_start"]),
            overwrite_overlaps=bool(data.get("overwrite_overlaps", False)),
            _source_track_id=data.get("source_track_id"),
            _previous_start=data.get("previous_start"),
            _displaced_clips=tuple(data.get("displaced_clips") or ()),
        )

    @staticmethod
    def create(
        clip_id: str,
        target_track_id: str,
        timeline_start: int,
        overwrite_overlaps: bool = False,
        label: str = "Move Subtitle Clip",
    ) -> "MoveSubtitleClipCommand":
        return MoveSubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            clip_id=clip_id,
            target_track_id=target_track_id,
            timeline_start=int(timeline_start),
            overwrite_overlaps=overwrite_overlaps,
        )


@dataclass(frozen=True)
class CopySubtitleClipCommand:
    """Duplicate a subtitle cue onto a subtitle track at an exact frame."""

    command_id: str
    label: str
    source_clip_id: str
    target_track_id: str
    timeline_start: int
    _copied_clip_id: str | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        source = timeline.get_subtitle_clip_by_id(self.source_clip_id)
        target_track = timeline.get_track_by_id(self.target_track_id)
        if source is None:
            raise ValueError(f"Subtitle clip not found: {self.source_clip_id}")
        _ensure_subtitle_clip_editable(timeline, source)
        if target_track is None or target_track.track_type != "subtitle":
            raise ValueError(f"Target is not a subtitle track: {self.target_track_id}")
        if target_track.is_locked:
            raise ValueError(
                f"Subtitle track is locked and cannot be edited: {target_track.track_id}"
            )
        copied_id = self._copied_clip_id or str(uuid.uuid4())
        copied = source._replace(
            clip_id=copied_id,
            track_id=target_track.track_id,
            timeline_start=max(0, int(self.timeline_start)),
        )
        updated = timeline.with_subtitle_clip_added(target_track.track_id, copied)
        object.__setattr__(self, "_copied_clip_id", copied_id)
        return updated

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._copied_clip_id is None:
            logger.warning("CopySubtitleClipCommand.undo: no copied cue id")
            return timeline
        return timeline.with_subtitle_clip_removed(self._copied_clip_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the source and persistent generated copy identifier."""
        return {
            "command_id": self.command_id,
            "command_type": "CopySubtitleClipCommand",
            "label": self.label,
            "source_clip_id": self.source_clip_id,
            "target_track_id": self.target_track_id,
            "timeline_start": self.timeline_start,
            "copied_clip_id": self._copied_clip_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CopySubtitleClipCommand":
        """Deserialize a subtitle copy command."""
        return CopySubtitleClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Copy Subtitle Clip"),
            source_clip_id=data["source_clip_id"],
            target_track_id=data["target_track_id"],
            timeline_start=int(data["timeline_start"]),
            _copied_clip_id=data.get("copied_clip_id"),
        )

    @staticmethod
    def create(
        source_clip_id: str,
        target_track_id: str,
        timeline_start: int,
    ) -> "CopySubtitleClipCommand":
        return CopySubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label="Copy Subtitle Clip",
            source_clip_id=source_clip_id,
            target_track_id=target_track_id,
            timeline_start=int(timeline_start),
        )


@dataclass(frozen=True)
class DeleteSubtitleClipCommand:
    """Command to delete a subtitle clip from its track."""
    command_id: str
    label: str
    clip_id: str
    _previous_clip: dict[str, Any] | None = None
    _track_id: str | None = None
    _previous_binding: dict[str, Any] | None = None
    _previous_graphic_clip: dict[str, Any] | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        clip = timeline.get_subtitle_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Subtitle clip not found: {self.clip_id}")
        _ensure_subtitle_clip_editable(timeline, clip)
        owner = timeline.find_subtitle_clip_owner_track(self.clip_id)
        object.__setattr__(self, '_previous_clip', clip.to_dict())
        object.__setattr__(self, '_track_id', owner.track_id if owner else None)
        new_timeline = timeline.with_subtitle_clip_removed(self.clip_id)
        binding = timeline.get_caption_graphic_binding(self.clip_id)
        if binding is not None:
            graphic = timeline.get_clip_by_id(binding.graphic_clip_id)
            object.__setattr__(self, "_previous_binding", binding.to_dict())
            object.__setattr__(self, "_previous_graphic_clip", graphic.to_dict() if graphic is not None else None)
            new_timeline = new_timeline.with_caption_graphic_binding_removed(binding.binding_id)
            if graphic is not None:
                new_timeline = new_timeline.with_clip_removed(graphic.clip_id)
        logger.debug(f"DeleteSubtitleClipCommand applied: removed {self.clip_id}")
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if not self._previous_clip or not self._track_id:
            logger.warning("DeleteSubtitleClipCommand.undo: no snapshot")
            return timeline
        clip = SubtitleClip.from_dict(self._previous_clip)
        new_timeline = timeline.with_subtitle_clip_added(self._track_id, clip)
        if self._previous_graphic_clip is not None and self._previous_binding is not None:
            graphic = ClipInstance.from_dict(self._previous_graphic_clip)
            new_timeline = new_timeline.with_clip_added(graphic.track_id, graphic)
            from src.features.edit.core.captions.graphics import CaptionGraphicBinding
            new_timeline = new_timeline.with_caption_graphic_binding_added(
                CaptionGraphicBinding.from_dict(self._previous_binding)
            )
        logger.debug(f"DeleteSubtitleClipCommand undone: restored {self.clip_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "DeleteSubtitleClipCommand",
            "label": self.label,
            "clip_id": self.clip_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeleteSubtitleClipCommand:
        return DeleteSubtitleClipCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Delete Subtitle Clip"),
            clip_id=data["clip_id"],
        )

    @staticmethod
    def create(clip_id: str) -> DeleteSubtitleClipCommand:
        return DeleteSubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Subtitle Clip",
            clip_id=clip_id,
        )
