"""SUB-P2-03/12: Caption-specific commands — metadata, style override, split, merge.

Phase 2A-2 adds the command layer the Caption Workspace needs. These commands
build on the Phase 0+1 subtitle command primitives (Add/Update/Delete) plus the
Phase 2A-1 layered model (CaptionStyle / CaptionCueMetadata):

* :class:`UpdateCaptionMetadataCommand` — change a cue's review metadata
  (speaker / status / note / lock / assignee / …). Routes through the existing
  ``UpdateSubtitleClipCommand`` so it shares the unified commit path
  (``TimelineView.execute_command`` → command stack → ``timeline_changed``),
  but exposes a typed, metadata-specific API so callers never hand-write raw
  field names.
* :class:`UpdateCaptionStyleOverrideCommand` — set/clear a cue's per-cue
  ``CaptionStyle`` override. Same commit path; this is how the Workspace's
  "apply preset to selection" / "clear override" actions reach the model.
* :class:`SplitSubtitleClipCommand` — split one cue at a frame into two cues
  that together cover the original span. Implemented as a :class:`MacroCommand`
  (delete + two adds) so undo is automatic and the SUB-P0-03 invariants are
  re-validated by the track on each add.
* :class:`MergeSubtitleClipsCommand` — merge two adjacent cues into one whose
  text/timing/style is taken from the first cue (configurable). Also a Macro.

All commands return the same ``(new_timeline, updated_cmd)`` contract the
command stack expects, so they compose inside other macros and undo/redo
correctly. They never touch ``edit_page._current_timeline`` directly — every
mutation goes through ``with_subtitle_clip_*`` which enforces invariants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from src.features.edit.core.timeline_v2 import TimelineSequence, ClipInstance, EditTrack
from src.features.edit.core.commands import (
    AddSubtitleClipCommand,
    CaptionEditMode,
    DeleteSubtitleClipCommand,
    MacroCommand,
    UpdateSubtitleClipCommand,
)
from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionTrackMetadata,
    CaptionStyle,
    EMPTY_STYLE,
    STYLE_FIELDS,
)
from src.features.edit.core.captions.graphics import (
    CaptionGraphicBinding,
    CaptionTrackGraphicStyle,
    cue_snapshot_for_graphic,
)


# ──────────────────────────────────────────────────────────────────────────
# UpdateCaptionMetadataCommand
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UpdateCaptionMetadataCommand:
    """Change a cue's :class:`CaptionCueMetadata` (speaker/status/note/lock/…).

    This is a thin, typed wrapper over :class:`UpdateSubtitleClipCommand`:
    apply/undo delegate entirely, so the command shares the unified commit
    path, undo snapshot semantics, and SUB-P0-03 invariant enforcement. The
    wrapper exists so Workspace code reads
    ``UpdateCaptionMetadataCommand.create(clip_id, metadata)`` rather than
    hand-writing ``UpdateSubtitleClipCommand(updates={"caption_metadata": ...})``.
    """

    command_id: str
    label: str
    clip_id: str
    metadata: CaptionCueMetadata
    _inner: UpdateSubtitleClipCommand = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, CaptionCueMetadata):
            raise TypeError("metadata must be a CaptionCueMetadata")
        if self._inner is None:
            object.__setattr__(self, "_inner", UpdateSubtitleClipCommand(
                command_id=self.command_id,
                label=self.label,
                clip_id=self.clip_id,
                updates={"caption_metadata": self.metadata},
            ))

    def apply(self, timeline: TimelineSequence):
        return self._inner.apply(timeline)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return self._inner.undo(timeline)

    @staticmethod
    def create(clip_id: str, metadata: CaptionCueMetadata) -> "UpdateCaptionMetadataCommand":
        return UpdateCaptionMetadataCommand(
            command_id=str(uuid.uuid4()),
            label="Update Caption Metadata",
            clip_id=clip_id,
            metadata=metadata,
        )


# ──────────────────────────────────────────────────────────────────────────
# UpdateCaptionStyleOverrideCommand
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UpdateCaptionStyleOverrideCommand:
    """Set or clear a cue's per-cue :class:`CaptionStyle` override.

    Passing ``style=None`` clears the override (the cue fully inherits its
    track's default_style). Passing a partial style overrides only those
    fields. Routes through :class:`UpdateSubtitleClipCommand` for the same
    unified commit path.
    """

    command_id: str
    label: str
    clip_id: str
    style: Optional[CaptionStyle]
    _inner: UpdateSubtitleClipCommand = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.style is not None and not isinstance(self.style, CaptionStyle):
            raise TypeError("style must be a CaptionStyle or None")
        if self._inner is None:
            object.__setattr__(self, "_inner", UpdateSubtitleClipCommand(
                command_id=self.command_id,
                label=self.label,
                clip_id=self.clip_id,
                updates={"style_override": self.style},
            ))

    def apply(self, timeline: TimelineSequence):
        return self._inner.apply(timeline)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return self._inner.undo(timeline)

    @staticmethod
    def create(clip_id: str, style: Optional[CaptionStyle]) -> "UpdateCaptionStyleOverrideCommand":
        return UpdateCaptionStyleOverrideCommand(
            command_id=str(uuid.uuid4()),
            label="Update Caption Style Override",
            clip_id=clip_id,
            style=style,
        )


@dataclass(frozen=True)
class UpdateSubtitleTrackStyleCommand:
    """Change a subtitle track's inherited/global style in one undo step."""

    command_id: str
    label: str
    track_id: str
    style: CaptionStyle
    _previous_style: Optional[CaptionStyle] = field(default=None, repr=False, compare=False)
    _previous_metadata: Optional[CaptionTrackMetadata] = field(default=None, repr=False, compare=False)
    _has_previous_metadata: bool = field(default=False, repr=False, compare=False)

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "subtitle":
            raise ValueError(f"Subtitle track not found: {self.track_id}")
        if track.is_locked:
            raise ValueError(f"Subtitle track is locked and cannot be edited: {self.track_id}")
        metadata = track.caption_metadata or CaptionTrackMetadata()
        previous = metadata.default_style
        changed_fields = tuple(
            field_name
            for field_name in STYLE_FIELDS
            if getattr(previous, field_name) != getattr(self.style, field_name)
        )
        # A global edit must not silently mutate a locked cue. Fully custom
        # cues are independent, while partial overrides still inherit the
        # fields that are None and therefore remain affected by this command.
        # AUD-15802: 零字段变更（提交与当前完全相同的全局样式，常见于样式
        # 对话框"未修改即确定"）不可能改变任何 cue——锁定校验必须放行，
        # 否则 override=None 的完全继承锁定 cue 会误触发整个命令抛错。
        if changed_fields:
            for cue in track.subtitle_clips:
                if not cue.is_edit_locked():
                    continue
                override = cue.style_override
                affected = (
                    override is None
                    or any(getattr(override, field_name) is None
                           for field_name in changed_fields)
                )
                if affected:
                    raise ValueError(
                        "Locked subtitle cue would be changed by global style edit: "
                        f"{cue.clip_id}"
                    )
        updated_track = track.with_caption_metadata(metadata.with_default_style(self.style))
        if self._previous_style is None:
            object.__setattr__(self, "_previous_style", previous)
        if not self._has_previous_metadata:
            object.__setattr__(self, "_previous_metadata", track.caption_metadata)
            object.__setattr__(self, "_has_previous_metadata", True)
        return timeline.with_track_updated(updated_track), self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "subtitle":
            return timeline
        if self._has_previous_metadata and self._previous_metadata is None:
            return timeline.with_track_updated(track.with_caption_metadata(None))
        metadata = track.caption_metadata or CaptionTrackMetadata()
        previous = self._previous_style or EMPTY_STYLE
        return timeline.with_track_updated(
            track.with_caption_metadata(metadata.with_default_style(previous))
        )

    @staticmethod
    def create(
        track_id: str,
        style: CaptionStyle,
        previous_style: Optional[CaptionStyle] = None,
        label: str = "Edit Global Subtitle Style",
    ) -> "UpdateSubtitleTrackStyleCommand":
        return UpdateSubtitleTrackStyleCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            track_id=track_id,
            style=style,
            _previous_style=previous_style,
        )


@dataclass(frozen=True)
class UpdateSubtitleTrackStylePatchCommand:
    """Apply only the supplied fields to a track's global caption style."""

    command_id: str
    label: str
    track_id: str
    style_patch: CaptionStyle
    _inner: Optional[UpdateSubtitleTrackStyleCommand] = field(
        default=None, repr=False, compare=False,
    )

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "subtitle":
            raise ValueError(f"Subtitle track not found: {self.track_id}")
        current = track.caption_metadata or CaptionTrackMetadata()
        merged = self.style_patch.merge_over(current.default_style)
        inner = UpdateSubtitleTrackStyleCommand.create(
            self.track_id, merged, label=self.label,
        )
        object.__setattr__(self, "_inner", inner)
        return inner.apply(timeline)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._inner is None:
            return timeline
        return self._inner.undo(timeline)

    @staticmethod
    def create(track_id: str, style_patch: CaptionStyle,
               label: str = "AI Update Global Subtitle Style") -> "UpdateSubtitleTrackStylePatchCommand":
        return UpdateSubtitleTrackStylePatchCommand(
            command_id=str(uuid.uuid4()), label=label,
            track_id=track_id, style_patch=style_patch,
        )


@dataclass(frozen=True)
class UpdateSubtitleTrackMetadataCommand:
    """Update language/role/label metadata for one subtitle track."""

    command_id: str
    label: str
    track_id: str
    updates: dict[str, Any]
    _previous_metadata: Optional[CaptionTrackMetadata] = field(default=None, repr=False, compare=False)

    _ALLOWED_FIELDS = frozenset({"language", "role", "safe_anchor", "label"})

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "subtitle":
            raise ValueError(f"Subtitle track not found: {self.track_id}")
        if track.is_locked:
            raise ValueError(f"Subtitle track is locked and cannot be edited: {self.track_id}")
        if not set(self.updates).issubset(self._ALLOWED_FIELDS):
            raise ValueError("Unsupported subtitle track metadata field")
        current = track.caption_metadata or CaptionTrackMetadata()
        updated = CaptionTrackMetadata(
            language=str(self.updates.get("language", current.language) or ""),
            role=str(self.updates.get("role", current.role) or current.role),
            safe_anchor=str(self.updates.get("safe_anchor", current.safe_anchor)
                            or current.safe_anchor),
            label=str(self.updates.get("label", current.label) or ""),
            default_style=current.default_style,
        )
        previous = self._previous_metadata
        if previous is None:
            object.__setattr__(self, "_previous_metadata", track.caption_metadata)
        return timeline.with_track_updated(
            track.with_caption_metadata(updated)
        ), self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "subtitle":
            return timeline
        return timeline.with_track_updated(
            track.with_caption_metadata(self._previous_metadata)
        )

    @staticmethod
    def create(track_id: str, updates: dict[str, Any],
               label: str = "Edit Subtitle Track Metadata") -> "UpdateSubtitleTrackMetadataCommand":
        return UpdateSubtitleTrackMetadataCommand(
            command_id=str(uuid.uuid4()), label=label,
            track_id=track_id, updates=dict(updates),
        )


# ──────────────────────────────────────────────────────────────────────────
# SplitSubtitleClipCommand
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SplitSubtitleClipCommand:
    """Split one cue into two at ``split_frame``.

    The original cue ``[start, end)`` becomes:
      * left cue:  ``[start, split_frame)`` with the original text
      * right cue: ``[split_frame, end)`` with empty text (the editor fills it)

    All other fields (style_override, caption_metadata, font, color, …) are
    copied verbatim to both halves — split is a geometric operation, not a
    restyle. Implemented as a MacroCommand (delete original + add two), so:
      * undo restores the original cue in one step,
      * SUB-P0-03 invariants (overlap / owner / unique id) are re-validated by
        the track on each add.

    ``split_frame`` must be strictly inside ``(start, end)``; equal to a boundary
    is a no-op-ish error (raises on apply, before mutating).
    """

    command_id: str
    label: str
    clip_id: str
    split_frame: int
    _macro: Optional[MacroCommand] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._macro is None:
            # Macro is built lazily in apply() once we can read the clip, because
            # we need the clip's full field snapshot + track_id to construct the
            # two AddSubtitleClipCommand children. Store split params only.
            pass

    def _build_macro(self, timeline: TimelineSequence) -> MacroCommand:
        clip = timeline.get_subtitle_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Subtitle clip not found: {self.clip_id}")
        if clip.is_edit_locked():
            raise ValueError(f"Subtitle clip is locked and cannot be split: {self.clip_id}")
        owner = timeline.find_subtitle_clip_owner_track(self.clip_id)
        if owner is None:
            raise ValueError(f"Owner track not found for clip: {self.clip_id}")
        if owner.is_locked:
            raise ValueError(
                f"Subtitle track is locked and cannot be edited: {owner.track_id}"
            )

        start = clip.timeline_start
        end = clip.timeline_end  # start + duration
        if not (start < self.split_frame < end):
            raise ValueError(
                f"split_frame {self.split_frame} must be strictly within "
                f"({start}, {end}) for clip {self.clip_id}"
            )

        left_duration = self.split_frame - start
        right_duration = end - self.split_frame

        # SUB-P2-01: stamp each half from the original clip via from_clip so
        # style_override + caption_metadata (speaker/status/note/…) carry to
        # the halves, not just text/timing. from_clip regenerates clip_id and
        # overrides timeline_start/duration/text; every other field is copied.
        # Delete original, then add two halves. INSERT mode avoids the
        # OVERWRITE neighbour-trimming logic (the two halves are by construction
        # non-overlapping and contiguous, but INSERT is the safe semantic).
        sub_commands = [
            DeleteSubtitleClipCommand.create(self.clip_id),
            AddSubtitleClipCommand.from_clip(
                owner.track_id, clip,
                timeline_start=start,
                duration=left_duration,
                text=clip.text,  # left keeps original text
                mode=CaptionEditMode.INSERT,
                label="Split: left half",
            ),
            AddSubtitleClipCommand.from_clip(
                owner.track_id, clip,
                timeline_start=self.split_frame,
                duration=right_duration,
                text="",  # right starts empty
                mode=CaptionEditMode.INSERT,
                label="Split: right half",
            ),
        ]
        return MacroCommand.create(self.label, sub_commands)

    def apply(self, timeline: TimelineSequence):
        macro = self._build_macro(timeline)
        object.__setattr__(self, "_macro", macro)
        return macro.apply(timeline)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._macro is None:
            # Should not happen after a successful apply, but guard anyway.
            return timeline
        return self._macro.undo(timeline)

    @staticmethod
    def create(clip_id: str, split_frame: int) -> "SplitSubtitleClipCommand":
        return SplitSubtitleClipCommand(
            command_id=str(uuid.uuid4()),
            label="Split Subtitle Clip",
            clip_id=clip_id,
            split_frame=split_frame,
        )


# ──────────────────────────────────────────────────────────────────────────
# MergeSubtitleClipsCommand
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MergeSubtitleClipsCommand:
    """Merge two adjacent cues into one.

    The resulting cue spans ``[first.start, second.end)`` and takes its text
    from ``first.text + separator + second.text`` (default separator: space).
    Style/metadata come from the ``primary`` clip (first by default); pass
    ``primary="second"`` to take the second cue's style instead.

    Both cues must:
      * live on the same track,
      * be adjacent (``first.end == second.start``) or overlapping/abutting
        within the track's no-overlap rule.

    Implemented as a MacroCommand (delete both + add one). Undo restores both
    originals in one step.

    Args:
        first_clip_id: the earlier cue (by timeline_start).
        second_clip_id: the later cue.
        separator: text inserted between the two texts (default " ").
        primary: which cue's style/metadata to keep — ``"first"`` or ``"second"``.
    """

    command_id: str
    label: str
    first_clip_id: str
    second_clip_id: str
    separator: str = " "
    primary: str = "first"
    _macro: Optional[MacroCommand] = field(default=None, repr=False, compare=False)

    def _build_macro(self, timeline: TimelineSequence) -> MacroCommand:
        first = timeline.get_subtitle_clip_by_id(self.first_clip_id)
        second = timeline.get_subtitle_clip_by_id(self.second_clip_id)
        if first is None:
            raise ValueError(f"First clip not found: {self.first_clip_id}")
        if second is None:
            raise ValueError(f"Second clip not found: {self.second_clip_id}")
        # AUD-15803: 同 clip 合并是调用方错误。旧实现靠宏内第二条 Delete 因
        # 片段已删而抛 "not found"，经宏回滚后用户看到的是删除失败而非
        # "不能合并自身"，报错误导。
        if self.first_clip_id == self.second_clip_id:
            raise ValueError(
                f"Cannot merge a clip with itself: {self.first_clip_id}"
            )
        locked_ids = [
            clip.clip_id for clip in (first, second) if clip.is_edit_locked()
        ]
        if locked_ids:
            raise ValueError(
                f"Locked subtitle clips cannot be merged: {', '.join(locked_ids)}"
            )

        owner_first = timeline.find_subtitle_clip_owner_track(self.first_clip_id)
        owner_second = timeline.find_subtitle_clip_owner_track(self.second_clip_id)
        if owner_first is None or owner_second is None:
            raise ValueError("Owner track not found for merge clips")
        if owner_first.is_locked or owner_second.is_locked:
            raise ValueError("Locked subtitle tracks cannot be merged")
        if owner_first.track_id != owner_second.track_id:
            raise ValueError(
                f"Cannot merge clips on different tracks: "
                f"{owner_first.track_id} vs {owner_second.track_id}"
            )
        track_id = owner_first.track_id

        # Order by timeline_start so "first" really is earlier.
        if first.timeline_start > second.timeline_start:
            first, second = second, first

        # AUD-15803: 相邻性校验——文档声明两条 cue 必须相邻/重叠/贴合
        # （second.start <= first.end）。中间有间隙的合并会静默吞掉间隙
        # （无字幕区间变为有字幕），必须显式拒绝。
        if second.timeline_start > first.timeline_end:
            raise ValueError(
                f"Cannot merge clips with a gap between them: "
                f"first ends at {first.timeline_end}, "
                f"second starts at {second.timeline_start}"
            )

        # SUB-P2-01: stamp the merged cue from the chosen style source via
        # from_clip so style_override + caption_metadata carry to the result,
        # not just text/timing. from_clip regenerates clip_id and overrides
        # timeline_start/duration/text; every other field is copied.
        style_src = first if self.primary == "first" else second
        merged_duration = second.timeline_end - first.timeline_start
        # Combine text. Preserve newlines if either side had them.
        ft = first.text or ""
        st = second.text or ""
        if ft and st:
            merged_text = ft + self.separator + st
        else:
            merged_text = ft or st

        sub_commands = [
            DeleteSubtitleClipCommand.create(self.first_clip_id),
            DeleteSubtitleClipCommand.create(self.second_clip_id),
            AddSubtitleClipCommand.from_clip(
                track_id, style_src,
                timeline_start=first.timeline_start,
                duration=merged_duration,
                text=merged_text,
                mode=CaptionEditMode.INSERT,
                label="Merge: result",
            ),
        ]
        return MacroCommand.create(self.label, sub_commands)

    def apply(self, timeline: TimelineSequence):
        macro = self._build_macro(timeline)
        object.__setattr__(self, "_macro", macro)
        return macro.apply(timeline)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._macro is None:
            return timeline
        return self._macro.undo(timeline)

    @staticmethod
    def create(
        first_clip_id: str,
        second_clip_id: str,
        separator: str = " ",
        primary: str = "first",
    ) -> "MergeSubtitleClipsCommand":
        if primary not in ("first", "second"):
            raise ValueError(f"primary must be 'first' or 'second', got {primary!r}")
        return MergeSubtitleClipsCommand(
            command_id=str(uuid.uuid4()),
            label="Merge Subtitle Clips",
            first_clip_id=first_clip_id,
            second_clip_id=second_clip_id,
            separator=separator,
            primary=primary,
        )


# ──────────────────────────────────────────────────────────────────────────
# Batch helpers (for Workspace multi-select operations)
# ──────────────────────────────────────────────────────────────────────────

def build_batch_metadata_update(
    clip_ids: list[str],
    metadata: CaptionCueMetadata,
) -> list[UpdateCaptionMetadataCommand]:
    """Build a list of metadata-update commands, one per clip_id.

    Feed the result to ``MacroCommand.create`` (or ``_execute_batch_macro``) for
    a single-undo batch. Every clip gets the *same* metadata object — callers
    wanting per-clip metadata should build their own list.
    """
    return [UpdateCaptionMetadataCommand.create(cid, metadata) for cid in clip_ids]


def build_batch_style_override(
    clip_ids: list[str],
    style: Optional[CaptionStyle],
) -> list[UpdateCaptionStyleOverrideCommand]:
    """Build a list of style-override commands, one per clip_id.

    Used by Workspace's "apply preset to selection" / "clear overrides" actions.
    """
    return [UpdateCaptionStyleOverrideCommand.create(cid, style) for cid in clip_ids]


# ──────────────────────────────────────────────────────────────────────────
# Caption graphic commands (P0/P1/P2/P4)
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AddCaptionGraphicBindingCommand:
    """Attach an already-copied graphic clip to a detached binding."""

    command_id: str
    label: str
    binding: CaptionGraphicBinding

    def apply(self, timeline: TimelineSequence):
        clip = timeline.get_clip_by_id(self.binding.graphic_clip_id)
        if clip is None:
            raise ValueError(f"Graphic clip missing: {self.binding.graphic_clip_id}")
        track = timeline.get_track_by_id(clip.track_id)
        if track is None or track.track_role != "graphics":
            raise ValueError("Copied caption graphic must stay on a graphics track")
        return timeline.with_caption_graphic_binding_added(self.binding), self

    def undo(self, timeline: TimelineSequence):
        return timeline.with_caption_graphic_binding_removed(self.binding.binding_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "AddCaptionGraphicBindingCommand",
            "label": self.label,
            "binding": self.binding.to_dict(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AddCaptionGraphicBindingCommand":
        return AddCaptionGraphicBindingCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Copied Caption Graphic Binding"),
            binding=CaptionGraphicBinding.from_dict(data.get("binding", {})),
        )

    @staticmethod
    def create(binding: CaptionGraphicBinding) -> "AddCaptionGraphicBindingCommand":
        return AddCaptionGraphicBindingCommand(
            command_id=str(uuid.uuid4()),
            label="Add Copied Caption Graphic Binding",
            binding=binding,
        )

@dataclass(frozen=True)
class PromoteSubtitleToGraphicCommand:
    """Materialize one cue on a compatible video graphics track."""

    command_id: str
    label: str
    clip_id: str
    template_id: str = "fade"
    template_version: str = "1"
    parameters: dict[str, Any] = field(default_factory=dict)
    composite_id: str | None = None
    sync_text: bool = True
    sync_timing: bool = True
    sync_style: bool = False
    binding_id: str | None = None
    graphic_clip_id: str | None = None
    _created_track_id: str | None = None
    _previous_display_mode: str | None = None

    def apply(self, timeline: TimelineSequence):
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        owner = timeline.find_subtitle_clip_owner_track(self.clip_id)
        if cue is None or owner is None:
            raise ValueError(f"Subtitle clip not found: {self.clip_id}")
        if owner.is_locked or cue.is_edit_locked():
            raise ValueError(f"Subtitle clip is locked and cannot be promoted: {self.clip_id}")
        if timeline.get_caption_graphic_binding(self.clip_id) is not None:
            raise ValueError(f"Subtitle clip already has a graphic binding: {self.clip_id}")

        binding_id = self.binding_id or str(uuid.uuid4())
        graphic_clip_id = self.graphic_clip_id or str(uuid.uuid4())
        graphics_track = timeline.get_graphics_track()
        created_track_id: str | None = None
        if graphics_track is None:
            created_track_id = str(uuid.uuid4())
            graphics_track = EditTrack(
                track_id=created_track_id,
                name="Graphics",
                track_type="video",
                track_role="graphics",
                index=len(timeline.video_tracks),
            )
            timeline = timeline.with_track_added(graphics_track)

        graphic_clip = ClipInstance(
            clip_id=graphic_clip_id,
            asset_id=f"caption-graphic:{graphic_clip_id}",
            track_id=graphics_track.track_id,
            timeline_start=cue.timeline_start,
            timeline_duration=cue.timeline_duration,
            source_start=0,
            source_duration=max(1, cue.timeline_duration),
            speed=1.0,
            opacity=1.0,
            composite_id=self.composite_id,
            label_color=cue.label_color,
        )
        try:
            timeline = timeline.with_clip_added(graphics_track.track_id, graphic_clip)
        except ValueError:
            # A video track cannot overlap regular clips. Use a second graphics
            # track for simultaneous cues rather than weakening that invariant.
            # AUD-15801: 若上面刚首建了仍为空的图形轨 A，必须先把它从时间线
            # 摘除再建兜底轨 B——否则 A 泄漏为空轨，而 _created_track_id 记录
            # 的是 B，undo 只清 B 不清 A。
            if created_track_id is not None:
                timeline = timeline.with_track_removed(created_track_id)
            fallback_track_id = str(uuid.uuid4())
            graphics_track = EditTrack(
                track_id=fallback_track_id,
                name=f"Graphics {len(timeline.video_tracks) + 1}",
                track_type="video",
                track_role="graphics",
                index=len(timeline.video_tracks),
            )
            timeline = timeline.with_track_added(graphics_track)
            graphic_clip = graphic_clip.with_track(graphics_track.track_id)
            timeline = timeline.with_clip_added(graphics_track.track_id, graphic_clip)
            created_track_id = fallback_track_id

        binding = CaptionGraphicBinding(
            binding_id=binding_id,
            caption_track_id=owner.track_id,
            caption_cue_id=cue.clip_id,
            graphic_clip_id=graphic_clip_id,
            composite_id=self.composite_id,
            template_id=self.template_id,
            template_version=self.template_version,
            parameters=dict(self.parameters),
            display_mode="graphic",
            sync_text=self.sync_text,
            sync_timing=self.sync_timing,
            sync_style=self.sync_style,
            text_snapshot=cue.text,
            style_snapshot=cue_snapshot_for_graphic(cue),
        )
        timeline = timeline.with_caption_graphic_binding_added(binding)
        object.__setattr__(self, "binding_id", binding_id)
        object.__setattr__(self, "graphic_clip_id", graphic_clip_id)
        object.__setattr__(self, "_created_track_id", created_track_id)
        object.__setattr__(self, "_previous_display_mode", cue.display_mode)
        updated_cue = cue.with_params(display_mode="graphic")
        timeline = timeline.with_subtitle_clip_updated(updated_cue)
        return timeline, self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self.binding_id:
            timeline = timeline.with_caption_graphic_binding_removed(self.binding_id)
        if self.graphic_clip_id:
            timeline = timeline.with_clip_removed(self.graphic_clip_id)
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        if cue is not None:
            timeline = timeline.with_subtitle_clip_updated(
                cue.with_params(display_mode=self._previous_display_mode or "native")
            )
        if self._created_track_id and timeline.get_track_by_id(self._created_track_id) is not None:
            track = timeline.get_track_by_id(self._created_track_id)
            if track is not None and not track.clips:
                timeline = timeline.with_track_removed(self._created_track_id)
        return timeline

    @staticmethod
    def create(clip_id: str, *, template_id: str = "fade",
               template_version: str = "1", parameters: dict[str, Any] | None = None,
               composite_id: str | None = None,
               sync_text: bool = True, sync_timing: bool = True,
               sync_style: bool = False) -> "PromoteSubtitleToGraphicCommand":
        return PromoteSubtitleToGraphicCommand(
            command_id=str(uuid.uuid4()), label="Convert Subtitle to Graphic",
            clip_id=clip_id, template_id=template_id,
            template_version=template_version, parameters=dict(parameters or {}),
            composite_id=composite_id,
            sync_text=sync_text, sync_timing=sync_timing, sync_style=sync_style,
        )


@dataclass(frozen=True)
class DetachCaptionGraphicCommand:
    """Keep the graphic clip but stop source-driven updates."""

    command_id: str
    label: str
    clip_id: str
    _previous_binding: CaptionGraphicBinding | None = None
    _previous_display_mode: str | None = None

    def apply(self, timeline: TimelineSequence):
        binding = timeline.get_caption_graphic_binding(self.clip_id)
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        if binding is None or cue is None:
            raise ValueError(f"Caption graphic binding not found: {self.clip_id}")
        object.__setattr__(self, "_previous_binding", binding)
        object.__setattr__(self, "_previous_display_mode", cue.display_mode)
        snapshot = {
            "text": cue.text,
            "style_fields": {field: getattr(cue, field) for field in STYLE_FIELDS},
            "style_override": cue.style_override.to_dict() if cue.style_override is not None else None,
        }
        timeline = timeline.with_caption_graphic_binding_updated(binding.detached(snapshot))
        return timeline.with_subtitle_clip_updated(cue.with_params(display_mode="native")), self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._previous_binding is not None:
            timeline = timeline.with_caption_graphic_binding_updated(self._previous_binding)
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        if cue is not None:
            timeline = timeline.with_subtitle_clip_updated(
                cue.with_params(display_mode=self._previous_display_mode or "graphic")
            )
        return timeline

    @staticmethod
    def create(clip_id: str) -> "DetachCaptionGraphicCommand":
        return DetachCaptionGraphicCommand(
            command_id=str(uuid.uuid4()), label="Detach Caption Graphic", clip_id=clip_id,
        )


@dataclass(frozen=True)
class DemoteGraphicToSubtitleCommand:
    """Remove the graphic representation and restore native subtitle display."""

    command_id: str
    label: str
    clip_id: str
    _binding: CaptionGraphicBinding | None = None
    _graphic_clip: dict[str, Any] | None = None
    _previous_display_mode: str | None = None
    # AUD-15800: apply 删除的空图形轨快照（undo 时按原样重建）
    _removed_track: dict[str, Any] | None = None

    def apply(self, timeline: TimelineSequence):
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        binding = timeline.get_caption_graphic_binding(self.clip_id)
        if cue is None or binding is None:
            raise ValueError(f"Caption graphic binding not found: {self.clip_id}")
        graphic = timeline.get_clip_by_id(binding.graphic_clip_id)
        if graphic is None:
            raise ValueError(f"Graphic clip missing: {binding.graphic_clip_id}")
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_graphic_clip", graphic.to_dict())
        object.__setattr__(self, "_previous_display_mode", cue.display_mode)
        timeline = timeline.with_caption_graphic_binding_removed(binding.binding_id)
        timeline = timeline.with_clip_removed(graphic.clip_id)
        graphics_track = timeline.get_track_by_id(graphic.track_id)
        removed_track_snapshot: dict[str, Any] | None = None
        if graphics_track is not None and graphics_track.track_role == "graphics" and not graphics_track.clips:
            removed_track_snapshot = graphics_track.to_dict()
            timeline = timeline.with_track_removed(graphics_track.track_id)
        object.__setattr__(self, "_removed_track", removed_track_snapshot)
        return timeline.with_subtitle_clip_updated(cue.with_params(display_mode="native")), self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._binding is None or self._graphic_clip is None:
            return timeline
        graphic = ClipInstance.from_dict(self._graphic_clip)
        # AUD-15800: apply 在图形轨清空后将其删除（Promote 自动创建的专用轨
        # 通常只含这一个片段）。旧实现此时因轨道缺失直接返回——绑定、图形
        # 片段、display_mode 全部未恢复，撤销无声失败。现按 apply 前快照
        # 原样重建轨道（快照缺失时镜像 Promote.apply 的建轨兜底）再回加。
        if timeline.get_track_by_id(graphic.track_id) is None:
            if self._removed_track is not None:
                restored_track = EditTrack.from_dict(self._removed_track)
            else:
                restored_track = EditTrack(
                    track_id=graphic.track_id,
                    name="Graphics",
                    track_type="video",
                    track_role="graphics",
                    index=len(timeline.video_tracks),
                )
            timeline = timeline.with_track_added(restored_track)
        timeline = timeline.with_clip_added(graphic.track_id, graphic)
        timeline = timeline.with_caption_graphic_binding_added(self._binding)
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        if cue is not None:
            timeline = timeline.with_subtitle_clip_updated(
                cue.with_params(display_mode=self._previous_display_mode or "graphic")
            )
        return timeline

    @staticmethod
    def create(clip_id: str) -> "DemoteGraphicToSubtitleCommand":
        return DemoteGraphicToSubtitleCommand(
            command_id=str(uuid.uuid4()), label="Convert Graphic to Subtitle", clip_id=clip_id,
        )


@dataclass(frozen=True)
class ApplyCaptionTrackTemplateCommand:
    """Apply/replace/remove a lazy track-level graphic template."""

    command_id: str
    label: str
    track_id: str
    style: CaptionTrackGraphicStyle | None
    _previous: CaptionTrackGraphicStyle | None = None

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None or track.track_type != "subtitle":
            raise ValueError(f"Subtitle track not found: {self.track_id}")
        if track.is_locked:
            raise ValueError(f"Subtitle track is locked: {self.track_id}")
        previous = next((item for item in timeline.caption_track_graphic_styles
                         if item.track_id == self.track_id), None)
        object.__setattr__(self, "_previous", previous)
        updated = timeline.without_caption_track_graphic_style(self.track_id)
        if self.style is not None:
            updated = updated.with_caption_track_graphic_style(self.style)
        return updated

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        updated = timeline.without_caption_track_graphic_style(self.track_id)
        return updated.with_caption_track_graphic_style(self._previous) if self._previous is not None else updated

    @staticmethod
    def create(track_id: str, template_id: str | None, *, parameters: dict[str, Any] | None = None) -> "ApplyCaptionTrackTemplateCommand":
        style = None if not template_id else CaptionTrackGraphicStyle(
            track_id=track_id, template_id=template_id, parameters=dict(parameters or {}),
        )
        return ApplyCaptionTrackTemplateCommand(
            command_id=str(uuid.uuid4()), label="Apply Caption Track Template",
            track_id=track_id, style=style,
        )


@dataclass(frozen=True)
class ApplyCaptionTrackTemplateBatchCommand:
    """Apply or remove one template across subtitle tracks atomically."""

    command_id: str
    label: str
    track_ids: tuple[str, ...]
    template_id: str | None
    parameters: dict[str, Any] = field(default_factory=dict)
    _macro: MacroCommand | None = field(default=None, repr=False, compare=False)

    def apply(self, timeline: TimelineSequence):
        commands = tuple(
            ApplyCaptionTrackTemplateCommand.create(
                track_id, self.template_id, parameters=self.parameters
            )
            for track_id in self.track_ids
        )
        macro = MacroCommand(
            command_id=self.command_id, label=self.label, sub_commands=commands
        )
        updated_timeline, updated_macro = macro.apply(timeline)
        object.__setattr__(self, "_macro", updated_macro)
        return updated_timeline, self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return self._macro.undo(timeline) if self._macro is not None else timeline

    @staticmethod
    def create(
        track_ids: tuple[str, ...] | list[str],
        template_id: str | None,
        *,
        parameters: dict[str, Any] | None = None,
        label: str = "Apply Caption Track Template",
    ) -> "ApplyCaptionTrackTemplateBatchCommand":
        return ApplyCaptionTrackTemplateBatchCommand(
            command_id=str(uuid.uuid4()), label=label,
            track_ids=tuple(dict.fromkeys(str(track_id) for track_id in track_ids)),
            template_id=template_id,
            parameters=dict(parameters or {}),
        )


@dataclass(frozen=True)
class SyncCaptionGraphicCommand:
    """Explicitly synchronize one binding after a manual source edit."""

    command_id: str
    label: str
    clip_id: str
    _previous: dict[str, Any] | None = None
    _previous_binding: CaptionGraphicBinding | None = None

    def apply(self, timeline: TimelineSequence):
        binding = timeline.get_caption_graphic_binding(self.clip_id)
        cue = timeline.get_subtitle_clip_by_id(self.clip_id)
        if binding is None or cue is None:
            raise ValueError(f"Caption graphic binding not found: {self.clip_id}")
        clip = timeline.get_clip_by_id(binding.graphic_clip_id)
        if clip is None:
            raise ValueError(f"Graphic clip missing: {binding.graphic_clip_id}")
        object.__setattr__(self, "_previous", {"timeline_start": clip.timeline_start, "timeline_duration": clip.timeline_duration})
        object.__setattr__(self, "_previous_binding", binding)
        updated_binding = binding._replace(
            text_snapshot=cue.text,
            style_snapshot=cue_snapshot_for_graphic(cue),
        )
        timeline = timeline.with_caption_graphic_binding_updated(updated_binding)
        return timeline.sync_caption_graphic_for_cue(self.clip_id)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if not self._previous:
            return timeline
        binding = timeline.get_caption_graphic_binding(self.clip_id)
        if binding is None:
            return timeline
        if self._previous_binding is not None:
            timeline = timeline.with_caption_graphic_binding_updated(self._previous_binding)
            binding = self._previous_binding
        clip = timeline.get_clip_by_id(binding.graphic_clip_id)
        if clip is None:
            return timeline
        return timeline.with_clip_updated(clip.with_timeline_start(self._previous["timeline_start"]).with_timeline_duration(self._previous["timeline_duration"]))

    @staticmethod
    def create(clip_id: str) -> "SyncCaptionGraphicCommand":
        return SyncCaptionGraphicCommand(
            command_id=str(uuid.uuid4()), label="Sync Caption Graphic", clip_id=clip_id,
        )


@dataclass(frozen=True)
class UpdateCaptionGraphicBindingCommand:
    """Update a graphic-caption template and its published parameters.

    Switching an already promoted cue must keep its graphic clip, source cue,
    sync policy and undo identity intact.  Re-promoting to change an effect
    used to create needless tracks/clips; this command is the atomic path.
    """

    command_id: str
    label: str
    clip_id: str
    parameters: dict[str, Any] | None = None
    template_id: str | None = None
    template_version: str | None = None
    sync_text: bool | None = None
    sync_timing: bool | None = None
    sync_style: bool | None = None
    _previous: CaptionGraphicBinding | None = None

    def apply(self, timeline: TimelineSequence):
        binding = timeline.get_caption_graphic_binding(self.clip_id)
        if binding is None:
            raise ValueError(f"Caption graphic binding not found: {self.clip_id}")
        object.__setattr__(self, "_previous", binding)
        changes: dict[str, Any] = {}
        if self.parameters is not None:
            changes["parameters"] = dict(self.parameters)
        if self.template_id is not None:
            changes["template_id"] = self.template_id
        if self.template_version is not None:
            changes["template_version"] = self.template_version
        if self.sync_text is not None:
            changes["sync_text"] = bool(self.sync_text)
        if self.sync_timing is not None:
            changes["sync_timing"] = bool(self.sync_timing)
        if self.sync_style is not None:
            changes["sync_style"] = bool(self.sync_style)
        updated = binding._replace(**changes)
        return timeline.with_caption_graphic_binding_updated(updated), self

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        return timeline.with_caption_graphic_binding_updated(self._previous) if self._previous is not None else timeline

    @staticmethod
    def create(
        clip_id: str,
        parameters: dict[str, Any] | None = None,
        *,
        template_id: str | None = None,
        template_version: str | None = None,
        sync_text: bool | None = None,
        sync_timing: bool | None = None,
        sync_style: bool | None = None,
    ) -> "UpdateCaptionGraphicBindingCommand":
        return UpdateCaptionGraphicBindingCommand(
            command_id=str(uuid.uuid4()), label="Update Caption Graphic Parameters",
            clip_id=clip_id,
            parameters=(dict(parameters) if parameters is not None else None),
            template_id=template_id,
            template_version=template_version,
            sync_text=sync_text, sync_timing=sync_timing, sync_style=sync_style,
        )
