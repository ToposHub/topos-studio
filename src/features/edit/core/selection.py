"""
Selection Module - Timeline Selection Management (P4).

This module manages selection state for timeline editing.
Selection can include clips, transitions, tracks, or multiple items.

Design Principles:
- Immutable selection state (frozen=True)
- No PyQt dependencies
- Selection changes emit signals in UI layer

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from enum import Enum


class SelectionType(Enum):
    """Type of selected item."""
    CLIP = "clip"
    TRANSITION = "transition"
    TRACK = "track"
    TIMELINE = "timeline"
    MARKER = "marker"
    NONE = "none"


@dataclass(frozen=True)
class SelectionItem:
    """
    A single selected item.

    Attributes:
        item_type: Type of the selected item
        item_id: Unique identifier of the item
        track_id: Track ID if applicable (clips, transitions)
        timeline_id: Timeline ID for context
    """

    item_type: SelectionType
    item_id: str
    track_id: str | None = None
    timeline_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize selection item."""
        return {
            "item_type": self.item_type.value,
            "item_id": self.item_id,
            "track_id": self.track_id,
            "timeline_id": self.timeline_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SelectionItem:
        """Deserialize selection item."""
        return SelectionItem(
            item_type=SelectionType(data.get("item_type", "none")),
            item_id=data.get("item_id", ""),
            track_id=data.get("track_id"),
            timeline_id=data.get("timeline_id"),
        )


@dataclass(frozen=True)
class TimelineSelection:
    """
    Timeline selection state.

    Manages the current selection in the timeline. Selection can be:
    - Single clip
    - Multiple clips (multi-select)
    - Transition
    - Track
    - Timeline (when nothing specific is selected)
    - Empty (no selection)

    Selection is used to:
    - Determine what Properties panel shows
    - Determine what commands are applicable
    - Highlight items in TimelineView

    Attributes:
        items: Set of selected items (tuple for immutability)
        is_range: Whether selection is a time range (for range delete)
        range_start: Range start frame (if is_range)
        range_end: Range end frame (if is_range)
        primary_item: Primary selection (for single selection context)
    """

    items: tuple[SelectionItem, ...] = field(default_factory=tuple)
    is_range: bool = False
    range_start: int | None = None
    range_end: int | None = None
    primary_item: SelectionItem | None = None

    # ==================== Invariants (INT-09) ====================

    @staticmethod
    def _identity(item: SelectionItem) -> tuple:
        """身份键：类型 + ID + 时间线上下文。同一对象重复入选只保留首个。"""
        return (item.item_type, item.item_id, item.timeline_id)

    def __post_init__(self) -> None:
        """INT-09：构造点统一去重并归一 primary。

        审计复现：Shift 框选把 list(existing)+list(new) 直接拼进 items，
        selected_clip_ids 出现 ['c','c'] 且 is_multi=True，后续批量复制/
        删除/属性命令可能重复处理同一对象。所有 factory/merge/from_dict
        都经过本构造，因此在这里强制不变量：items 按 (类型, ID, 时间线)
        去重保持首现顺序；primary 不在去重后的 items 里时回落到首项。
        """
        if not self.items:
            if self.primary_item is not None:
                object.__setattr__(self, "primary_item", None)
            return
        deduped: list[SelectionItem] = []
        seen: set[tuple] = set()
        for item in self.items:
            key = self._identity(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        if len(deduped) != len(self.items):
            object.__setattr__(self, "items", tuple(deduped))
        if self.primary_item is not None and self._identity(self.primary_item) not in seen:
            object.__setattr__(self, "primary_item", deduped[0])

    # ==================== Computed Properties ====================

    @property
    def is_empty(self) -> bool:
        """Check if selection is empty."""
        return len(self.items) == 0 and not self.is_range

    @property
    def is_single(self) -> bool:
        """Check if single item is selected."""
        return len(self.items) == 1

    @property
    def is_multi(self) -> bool:
        """Check if multiple items are selected."""
        return len(self.items) > 1

    @property
    def selected_clip_ids(self) -> tuple[str, ...]:
        """Get IDs of selected clips."""
        return tuple(
            item.item_id for item in self.items
            if item.item_type == SelectionType.CLIP
        )

    @property
    def selected_transition_ids(self) -> tuple[str, ...]:
        """Get IDs of selected transitions."""
        return tuple(
            item.item_id for item in self.items
            if item.item_type == SelectionType.TRANSITION
        )

    @property
    def selected_track_ids(self) -> tuple[str, ...]:
        """Get IDs of selected tracks."""
        return tuple(
            item.item_id for item in self.items
            if item.item_type == SelectionType.TRACK
        )

    @property
    def selection_type(self) -> SelectionType:
        """Get the primary selection type."""
        if self.is_empty:
            return SelectionType.NONE
        if self.is_range:
            return SelectionType.TIMELINE
        if self.primary_item:
            return self.primary_item.item_type
        if self.items:
            # Return first item's type for multi-selection
            return self.items[0].item_type
        return SelectionType.NONE

    # ==================== Factory Methods ====================

    def with_clip_selected(
        self,
        clip_id: str,
        track_id: str,
        timeline_id: str,
        replace: bool = True
    ) -> TimelineSelection:
        """
        Create new selection with clip selected.

        Args:
            clip_id: Clip to select
            track_id: Track containing clip
            timeline_id: Timeline context
            replace: Replace existing selection (True) or add to it (False)

        Returns:
            New TimelineSelection
        """
        item = SelectionItem(
            item_type=SelectionType.CLIP,
            item_id=clip_id,
            track_id=track_id,
            timeline_id=timeline_id,
        )

        if replace:
            return TimelineSelection(
                items=(item,),
                is_range=False,
                primary_item=item,
            )
        else:
            # Add to existing selection（INT-09：按身份键替换，不误删跨类型同 ID 项）
            key = self._identity(item)
            new_items = tuple(
                i for i in self.items
                if self._identity(i) != key
            ) + (item,)
            return TimelineSelection(
                items=new_items,
                is_range=False,
                primary_item=item,  # New item becomes primary
            )

    def with_transition_selected(
        self,
        transition_id: str,
        track_id: str,
        timeline_id: str,
        replace: bool = True
    ) -> TimelineSelection:
        """
        Create new selection with transition selected.
        """
        item = SelectionItem(
            item_type=SelectionType.TRANSITION,
            item_id=transition_id,
            track_id=track_id,
            timeline_id=timeline_id,
        )

        if replace:
            return TimelineSelection(
                items=(item,),
                is_range=False,
                primary_item=item,
            )
        else:
            key = self._identity(item)
            new_items = tuple(
                i for i in self.items
                if self._identity(i) != key
            ) + (item,)
            return TimelineSelection(
                items=new_items,
                is_range=False,
                primary_item=item,
            )

    def with_track_selected(
        self,
        track_id: str,
        timeline_id: str,
        replace: bool = True
    ) -> TimelineSelection:
        """
        Create new selection with track selected.
        """
        item = SelectionItem(
            item_type=SelectionType.TRACK,
            item_id=track_id,
            timeline_id=timeline_id,
        )

        if replace:
            return TimelineSelection(
                items=(item,),
                is_range=False,
                primary_item=item,
            )
        else:
            key = self._identity(item)
            new_items = tuple(
                i for i in self.items
                if self._identity(i) != key
            ) + (item,)
            return TimelineSelection(
                items=new_items,
                is_range=False,
                primary_item=item,
            )

    def with_range_selected(
        self,
        start_frame: int,
        end_frame: int
    ) -> TimelineSelection:
        """
        Create new selection with time range selected.
        """
        return TimelineSelection(
            items=(),
            is_range=True,
            range_start=start_frame,
            range_end=end_frame,
            primary_item=None,
        )

    def with_item_removed(self, item_id: str) -> TimelineSelection:
        """
        Create new selection without specified item.
        """
        new_items = tuple(i for i in self.items if i.item_id != item_id)

        # Update primary item
        new_primary = self.primary_item
        if self.primary_item and self.primary_item.item_id == item_id:
            new_primary = new_items[0] if new_items else None

        return TimelineSelection(
            items=new_items,
            is_range=self.is_range and len(new_items) == 0,
            range_start=self.range_start,
            range_end=self.range_end,
            primary_item=new_primary,
        )

    def clear(self) -> TimelineSelection:
        """
        Create empty selection.
        """
        return TimelineSelection()

    def toggle_clip(
        self,
        clip_id: str,
        track_id: str,
        timeline_id: str
    ) -> TimelineSelection:
        """
        Toggle clip selection (add if not selected, remove if selected).
        """
        # Check if already selected
        for item in self.items:
            if item.item_id == clip_id and item.item_type == SelectionType.CLIP:
                return self.with_item_removed(clip_id)

        # Add to selection
        return self.with_clip_selected(
            clip_id, track_id, timeline_id, replace=False
        )

    def toggle_transition(
        self,
        transition_id: str,
        track_id: str,
        timeline_id: str
    ) -> TimelineSelection:
        """
        Toggle transition selection (add if not selected, remove if selected).
        """
        # Check if already selected
        for item in self.items:
            if item.item_id == transition_id and item.item_type == SelectionType.TRANSITION:
                return self.with_item_removed(transition_id)

        # Add to selection
        return self.with_transition_selected(
            transition_id, track_id, timeline_id, replace=False
        )

    # ==================== Query Methods ====================

    def contains_clip(self, clip_id: str) -> bool:
        """Check if clip is in selection."""
        return any(
            item.item_id == clip_id and item.item_type == SelectionType.CLIP
            for item in self.items
        )

    def contains_transition(self, transition_id: str) -> bool:
        """Check if transition is in selection."""
        return any(
            item.item_id == transition_id and item.item_type == SelectionType.TRANSITION
            for item in self.items
        )

    def contains_track(self, track_id: str) -> bool:
        """Check if track is in selection."""
        return any(
            item.item_id == track_id and item.item_type == SelectionType.TRACK
            for item in self.items
        )

    def get_selected_clips_by_track(self, track_id: str) -> tuple[str, ...]:
        """Get selected clip IDs for a specific track."""
        return tuple(
            item.item_id for item in self.items
            if item.item_type == SelectionType.CLIP and item.track_id == track_id
        )

    # ==================== Serialization ====================

    def to_dict(self) -> dict[str, Any]:
        """Serialize selection."""
        return {
            "items": [item.to_dict() for item in self.items],
            "is_range": self.is_range,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "primary_item": self.primary_item.to_dict() if self.primary_item else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineSelection:
        """Deserialize selection."""
        items = tuple(
            SelectionItem.from_dict(item_data)
            for item_data in data.get("items", [])
        )

        primary_data = data.get("primary_item")
        primary_item = SelectionItem.from_dict(primary_data) if primary_data else None

        return TimelineSelection(
            items=items,
            is_range=data.get("is_range", False),
            range_start=data.get("range_start"),
            range_end=data.get("range_end"),
            primary_item=primary_item,
        )

    # ==================== Equality ====================

    def __eq__(self, other: object) -> bool:
        """Field-complete, order-sensitive selection equality.

        AUD-18204/AUD-18205: the previous implementation compared only a
        set of ``item_id`` values, ignoring ``primary_item``, item types
        and item order — semantically different selections (CLIP vs TRACK
        with the same id, different primary in a multi-select) compared
        equal, and the dataclass-generated hash disagreed with ``__eq__``.
        Equality now covers every semantic field so selections can safely
        be used in sets/dicts and as change-detection keys.
        """
        if not isinstance(other, TimelineSelection):
            return NotImplemented

        # Ordered comparison of full items (type + id + track/timeline
        # context + order). Order matters for primary/anchor semantics.
        if self.items != other.items:
            return False

        # Primary item participates: re-anchoring a multi-select is a
        # selection change (e.g. with_clip_selected replace=False).
        if self.primary_item != other.primary_item:
            return False

        # Range semantics.
        if self.is_range != other.is_range:
            return False
        if self.is_range:
            if self.range_start != other.range_start:
                return False
            if self.range_end != other.range_end:
                return False

        return True

    def __hash__(self) -> int:
        """Hash consistent with ``__eq__`` (AUD-18204).

        Covers exactly the fields equality compares; range bounds are
        folded in only when ``is_range`` is true so equal selections
        always hash identically.
        """
        return hash((
            self.items,
            self.primary_item,
            self.is_range,
            self.range_start if self.is_range else None,
            self.range_end if self.is_range else None,
        ))

    def __repr__(self) -> str:
        if self.is_empty:
            return "TimelineSelection(empty)"
        if self.is_range:
            return f"TimelineSelection(range=[{self.range_start}-{self.range_end}])"
        if self.is_single:
            item = self.items[0]
            return f"TimelineSelection({item.item_type.value}={item.item_id})"
        return f"TimelineSelection({len(self.items)} items)"


# ================================================================
# Selection Helpers
# ================================================================

def select_all_clips_in_track(
    track_id: str,
    clip_ids: tuple[str, ...],
    timeline_id: str
) -> TimelineSelection:
    """
    Create selection with all clips in a track.

    Args:
        track_id: Track ID
        clip_ids: List of clip IDs to select
        timeline_id: Timeline context

    Returns:
        TimelineSelection with all clips
    """
    items = tuple(
        SelectionItem(
            item_type=SelectionType.CLIP,
            item_id=clip_id,
            track_id=track_id,
            timeline_id=timeline_id,
        )
        for clip_id in clip_ids
    )

    return TimelineSelection(
        items=items,
        is_range=False,
        primary_item=items[0] if items else None,
    )


def select_clips_in_range(
    track_id: str,
    clips: tuple[Any, ...],  # ClipInstance
    start_frame: int,
    end_frame: int,
    timeline_id: str
) -> TimelineSelection:
    """
    Select all clips that intersect with a time range.

    Args:
        track_id: Track ID
        clips: ClipInstance objects
        start_frame: Range start
        end_frame: Range end
        timeline_id: Timeline context

    Returns:
        TimelineSelection with clips in range
    """
    items = []
    for clip in clips:
        # Check if clip intersects with range
        if clip.timeline_start < end_frame and clip.timeline_end > start_frame:
            items.append(SelectionItem(
                item_type=SelectionType.CLIP,
                item_id=clip.clip_id,
                track_id=track_id,
                timeline_id=timeline_id,
            ))

    return TimelineSelection(
        items=tuple(items),
        is_range=False,
        primary_item=items[0] if items else None,
    )