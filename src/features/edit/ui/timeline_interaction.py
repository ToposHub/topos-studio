"""
Timeline Interaction State Machine.

Provides a clean state machine for tracking the current user interaction
in the timeline view. Only one interaction can be active at a time.

Design Principles (P2 requirements):
- P2-1: Clear event ownership (View handles global areas, ClipItem handles clip body/edge)
- P2-2: ClipDragSession tracks primary/linked/selected group
- P2-3: Preview-only principle (mouse move only changes preview, no commands)
- P2-4: Release commits one command
- P2-5: Esc cancel support
- P2-6: Drag threshold (small click-vs-drag guard before entering drag)
- P2-7: Cursor state for different zones

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from PyQt6.QtCore import QPointF


class InteractionState(Enum):
    """States the timeline interaction can be in."""
    IDLE = auto()
    SELECTING = auto()          # Rubber band selection
    DRAGGING_CLIP = auto()      # Moving a clip
    TRIMMING_LEFT = auto()      # Trimming clip left edge
    TRIMMING_RIGHT = auto()     # Trimming clip right edge
    SPLITTING = auto()          # Blade tool active
    PANNING = auto()            # Hand tool / middle mouse pan
    ZOOMING = auto()            # Zoom tool
    RESIZING_TRACK = auto()     # Adjusting track height
    RESIZING_AREA = auto()      # Adjusting V/A splitter
    DRAGGING_PLAYHEAD = auto()  # Scrubbing playhead
    DRAGGING_RULER = auto()     # Scrubbing via ruler
    SLIPPING = auto()           # Slip tool
    SLIDING = auto()            # Slide tool
    RIPPLE_TRIMMING_LEFT = auto()
    RIPPLE_TRIMMING_RIGHT = auto()
    ROLLING = auto()            # Roll trim
    DRAGGING_FADE_IN = auto()
    DRAGGING_FADE_OUT = auto()
    CONTEXT_MENU = auto()       # Context menu is open


# P2-6: Default drag threshold in pixels before a press becomes a drag.
# Keep this small so the first intentional pointer movement feels immediate;
# click selection is still protected because the threshold is not zero.
DEFAULT_DRAG_THRESHOLD_PX: float = 2.0


@dataclass(frozen=True)
class MoveGroupMember:
    """Single clip in a move group."""
    clip_id: str
    track_id: str
    old_start: int
    new_start: int
    new_track_id: str = ""  # Target track for cross-track moves (empty = same track)


@dataclass(frozen=True)
class MoveGroup:
    """Atomic move group for linked + selected clips."""
    primary_clip_id: str
    members: tuple[MoveGroupMember, ...]
    delta_frames: int

    @property
    def is_valid(self) -> bool:
        """All members have valid (non-negative) new_start."""
        return all(m.new_start >= 0 for m in self.members)

    @property
    def has_track_change(self) -> bool:
        """Check if any member is moving to a different track."""
        return any(m.new_track_id and m.new_track_id != m.track_id for m in self.members)


@dataclass
class ClipDragSession:
    """
    P2-2: Tracks a clip drag session including primary, linked, and selected group.

    Used by the view to coordinate multi-clip drag operations with preview-only
    updates during mouse move. The session stores original positions so that
    cancel (Esc) can restore everything without committing any command.

    P3: Extended with MoveGroup support for atomic linked + multi-select moves.
    P4: Extended with track change support for auto-create track on drag.
    """
    primary_clip_id: str
    linked_clip_ids: list[str] = field(default_factory=list)
    selected_clip_ids: list[str] = field(default_factory=list)
    start_scene_pos: QPointF = field(default_factory=lambda: QPointF())
    start_frame: int = 0
    current_frame: int = 0
    drag_threshold_px: float = DEFAULT_DRAG_THRESHOLD_PX
    threshold_passed: bool = False
    original_positions: dict[str, int] = field(default_factory=dict)  # clip_id -> start_frame
    # P4: Track positions for cross-track drag support
    original_track_ids: dict[str, str] = field(default_factory=dict)  # clip_id -> original track_id
    current_track_ids: dict[str, str] = field(default_factory=dict)  # clip_id -> current target track_id
    # Alt-drag copy mode
    copy_mode: bool = False
    # P3: MoveGroup for atomic validation
    move_group: Optional[MoveGroup] = None
    # Hot-path cache for drag preview; avoids recomputing unchanged previews.
    snap_points: tuple[int, ...] = ()
    last_preview_key: tuple | None = None
    # Track hit geometry is stable for the lifetime of a drag. Caching it
    # avoids rebuilding ordered track lists and reading every QRectF on each
    # mouse-move event, which is especially noticeable when crossing lanes.
    drag_track_bounds_by_type: dict[str, tuple[tuple[str, float, float], ...]] = field(
        default_factory=dict
    )


@dataclass
class InteractionSession:
    """
    Tracks the current interaction session.

    Rules:
    - Only one interaction can be active at a time.
    - Esc key cancels the current interaction and returns to IDLE.
    - Mouse release completes the interaction and returns to IDLE.
    - During drag/trim, model is NOT modified -- only preview items change.
    - Command is submitted only on mouse release (P2-3, P2-4).
    """
    state: InteractionState = InteractionState.IDLE
    start_pos: QPointF = field(default_factory=QPointF)
    current_pos: QPointF = field(default_factory=QPointF)
    session_id: int = 0
    # Clip being operated on
    primary_clip_id: Optional[str] = None
    primary_track_id: Optional[str] = None
    # For moves: original position before drag
    original_start: int = 0
    original_duration: int = 0
    original_source_start: int = 0
    original_source_duration: int = 0
    # For area/track resize
    original_value: float = 0.0
    # P2-6: Drag threshold support
    drag_threshold_px: float = DEFAULT_DRAG_THRESHOLD_PX
    threshold_passed: bool = False
    # P2-2: Clip drag session (set when dragging clips)
    clip_drag_session: Optional[ClipDragSession] = None

    @property
    def is_idle(self) -> bool:
        return self.state == InteractionState.IDLE

    @property
    def is_dragging(self) -> bool:
        return self.state in (
            InteractionState.DRAGGING_CLIP,
            InteractionState.TRIMMING_LEFT,
            InteractionState.TRIMMING_RIGHT,
            InteractionState.SLIPPING,
            InteractionState.SLIDING,
            InteractionState.RIPPLE_TRIMMING_LEFT,
            InteractionState.RIPPLE_TRIMMING_RIGHT,
            InteractionState.ROLLING,
            InteractionState.DRAGGING_FADE_IN,
            InteractionState.DRAGGING_FADE_OUT,
        )

    def begin(self, state: InteractionState, start_pos: QPointF, **kwargs) -> None:
        """Start a new interaction session."""
        self.state = state
        self.start_pos = start_pos
        self.current_pos = start_pos
        self.session_id += 1
        # P2-6: Reset threshold on new session
        self.threshold_passed = False
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def update_pos(self, pos: QPointF) -> None:
        """Update current position during interaction."""
        self.current_pos = pos
        # P2-6: Check drag threshold
        if not self.threshold_passed:
            dx = pos.x() - self.start_pos.x()
            dy = pos.y() - self.start_pos.y()
            dist = math.sqrt(dx * dx + dy * dy)
            if dist >= self.drag_threshold_px:
                self.threshold_passed = True

    def cancel(self) -> None:
        """Cancel current interaction (Esc key). Resets all state to idle."""
        self.state = InteractionState.IDLE
        self.primary_clip_id = None
        self.primary_track_id = None
        self.original_start = 0
        self.original_duration = 0
        self.original_source_start = 0
        self.original_source_duration = 0
        self.original_value = 0.0
        self.threshold_passed = False
        self.clip_drag_session = None

    def finish(self) -> None:
        """Finish interaction (mouse release). Resets all state to idle."""
        self.state = InteractionState.IDLE
        self.primary_clip_id = None
        self.primary_track_id = None
        self.threshold_passed = False
        self.clip_drag_session = None

    def delta_x(self) -> float:
        """Horizontal pixel delta since start."""
        return self.current_pos.x() - self.start_pos.x()

    def delta_y(self) -> float:
        """Vertical pixel delta since start."""
        return self.current_pos.y() - self.start_pos.y()

    def delta_frames(self, pixels_per_frame: float) -> int:
        """Frame delta since start."""
        return int(self.delta_x() / pixels_per_frame) if pixels_per_frame > 0 else 0
