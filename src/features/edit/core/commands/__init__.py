"""
Edit Commands Module - Command Pattern for Timeline Editing (P4).

This module implements the command pattern for all timeline editing operations.
Every edit operation is encapsulated as a command that supports undo/redo.

Design Principles:
- All commands are immutable
- Commands return new data instances (never mutate input)
- CommandStack manages undo/redo history
- Commands are independent of PyQt (pure data operations)

Commands:
- AddClipCommand: Add clip to timeline
- DeleteClipCommand: Remove clip from timeline
- MoveClipCommand: Move clip position
- TrimClipCommand: Trim clip in/out points
- RetimeMotionGraphicClipCommand: Retimes a scene-backed Motion Graphic
- SplitClipCommand: Split clip at position

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P4
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, Any, Callable, Mapping
import uuid

import logging

from src.features.edit.core.timeline_v2 import TimelineSequence, ClipInstance

from src.features.edit.core.project import EditProject

logger = logging.getLogger(__name__)


# ================================================================
# Command Protocol
# ================================================================

class EditCommand(Protocol):
    """
    Protocol for edit commands.

    All commands must implement:
    - command_id: Unique identifier
    - label: Human-readable label
    - apply(): Execute the command
    - undo(): Reverse the command
    """

    command_id: str
    label: str

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """Apply the command to timeline, return new timeline."""
        ...

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """Undo the command, restore previous state."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Serialize command for storage."""
        ...


# ================================================================
# CommandStack - Undo/Redo Management
# ================================================================

class CommandStack:
    """
    Manages undo/redo history for edit commands.

    The stack maintains:
    - A list of applied commands (for undo)
    - A list of undone commands (for redo)
    - A maximum history size to prevent memory issues

    Usage:
        stack = CommandStack()
        timeline = stack.execute(AddClipCommand.create(...), timeline)
        timeline = stack.undo(timeline)  # Remove clip
        timeline = stack.redo(timeline)  # Add clip back
    """

    def __init__(self, max_size: int = 100) -> None:
        """
        Initialize command stack.

        Args:
            max_size: Maximum number of commands to keep in history
        """
        self._undo_stack: list[EditCommand] = []
        self._redo_stack: list[EditCommand] = []
        self._max_size: int = max_size

    def execute(self, command: EditCommand, timeline: TimelineSequence) -> TimelineSequence:
        """
        Execute a command and add to history.

        Args:
            command: Command to execute
            timeline: Current timeline

        Returns:
            New timeline after command applied
        """
        # Apply command
        result = command.apply(timeline)

        # Commands may return (new_timeline, updated_command) tuple
        if isinstance(result, tuple):
            new_timeline, updated_command = result
            pushed_command = updated_command
        else:
            new_timeline = result
            pushed_command = command

        # R12/APP-01：no-op 不改历史。模型命令约定"无内容变化返回原
        # 实例"（MoveClip 原位等）；同一性即 no-op——不 push、不清
        # Redo（审计实测原位 Move 丢掉了之前的 redo 栈）。
        if new_timeline is timeline:
            logger.debug(
                f"CommandStack: no-op {command.label}, history unchanged"
            )
            return new_timeline

        self._undo_stack.append(pushed_command)

        # Clear redo stack (new action invalidates redo history)
        self._redo_stack.clear()

        # Trim history if needed
        if len(self._undo_stack) > self._max_size:
            self._undo_stack = self._undo_stack[-self._max_size:]

        logger.debug(
            f"CommandStack: executed {command.label}, "
            f"undo_stack size: {len(self._undo_stack)}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence | None:
        """
        Undo the last command.

        Args:
            timeline: Current timeline

        Returns:
            New timeline after undo, or None if nothing to undo
        """
        if not self._undo_stack:
            logger.debug("CommandStack: nothing to undo")
            return None

        command = self._undo_stack.pop()
        # AUD-16204: 先 pop 后执行的丢失路径——undo 抛异常时命令既不在
        # undo 栈也不在 redo 栈（从此不可撤销也不可重做），且调用方拿到
        # 异常后时间线引用未更新，栈内容与时间线状态错位。失败必须把
        # 命令回填回 undo 栈再原样上抛。
        try:
            new_timeline = command.undo(timeline)
        except Exception:
            self._undo_stack.append(command)
            raise
        self._redo_stack.append(command)

        logger.debug(
            f"CommandStack: undone {command.label}, "
            f"redo_stack size: {len(self._redo_stack)}"
        )
        return new_timeline

    def redo(self, timeline: TimelineSequence) -> TimelineSequence | None:
        """
        Redo the last undone command.

        Args:
            timeline: Current timeline

        Returns:
            New timeline after redo, or None if nothing to redo
        """
        if not self._redo_stack:
            logger.debug("CommandStack: nothing to redo")
            return None

        command = self._redo_stack.pop()
        # AUD-16204（redo 侧同型）：apply 抛异常时把命令回填回 redo 栈
        # 再上抛，禁止栈内容与时间线状态错位。
        try:
            result = command.apply(timeline)
        except Exception:
            self._redo_stack.append(command)
            raise
        # Commands may return (new_timeline, updated_command) tuple
        if isinstance(result, tuple):
            new_timeline, updated_command = result
            self._undo_stack.append(updated_command)
        else:
            new_timeline = result
            self._undo_stack.append(command)

        logger.debug(
            f"CommandStack: redone {command.label}, "
            f"undo_stack size: {len(self._undo_stack)}"
        )
        return new_timeline

    def can_undo(self) -> bool:
        """Check if undo is available."""
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        """Check if redo is available."""
        return len(self._redo_stack) > 0

    def get_undo_label(self) -> str | None:
        """Get label of next undoable command."""
        if self._undo_stack:
            return self._undo_stack[-1].label
        return None

    def get_redo_label(self) -> str | None:
        """Get label of next redoable command."""
        if self._redo_stack:
            return self._redo_stack[-1].label
        return None

    def clear(self) -> None:
        """Clear all history."""
        self._undo_stack.clear()
        self._redo_stack.clear()
        logger.debug("CommandStack: cleared")

    def get_history_size(self) -> int:
        """Get total history size."""
        return len(self._undo_stack) + len(self._redo_stack)


# ================================================================
# ProjectCommandStack - Project-level Undo/Redo
# ================================================================

class ProjectCommandStack:
    """
    Manages undo/redo history for project-level commands (Bin operations).

    Similar to CommandStack but operates on EditProject instead of TimelineSequence.
    """

    def __init__(self, max_size: int = 100) -> None:
        """
        Initialize command stack.

        Args:
            max_size: Maximum number of commands to keep in history
        """
        self._undo_stack: list = []  # List of project commands
        self._redo_stack: list = []
        self._max_size: int = max_size
        # N20/RF04：最近一次事务的序列更新（调用方消费后落缓存）
        self.last_sequence_updates: dict = {}
        self.sequence_provider: Callable[[], Mapping[str, TimelineSequence]] | None = None

    def _validate_sequence_payload(self, command: Any, *, undo: bool = False) -> None:
        validate = getattr(command, "validate_sequences", None)
        if callable(validate) and self.sequence_provider is not None:
            validate(self.sequence_provider(), undo=undo)

    def peek_history_command(self, *, redo: bool = False) -> Any | None:
        """Inspect the next command without removing it from history."""
        history = self._redo_stack if redo else self._undo_stack
        return history[-1] if history else None

    def execute(self, command: Any, project: EditProject) -> EditProject:
        """
        Execute a command and add to history.

        Args:
            command: Project command to execute
            project: Current project

        Returns:
            New project after command applied
        """
        self._validate_sequence_payload(command)
        result = command.apply(project)
        # N20/RF04：项目级事务命令返回结构化 ProjectTransactionResult
        # （含 sequence_map），栈只消费 project 并把 sequence_map 暴露
        # 给调用方落缓存；普通命令仍直接返回 EditProject。
        # BE-01：经 consume_project_result 统一归一——二元组（EditCommand
        # 兼容）不再被误当工程 replace，栈持有含 apply 期快照的
        # updated_command，undo 拿得到激活前内容。
        new_project, stack_command = consume_project_result(command, result)
        if isinstance(result, ProjectTransactionResult):
            self.last_sequence_updates = dict(result.sequence_map)
        else:
            self.last_sequence_updates = {}
        # R25/F-R06：发布 revision 单调推进——此前 execute/undo/redo 都
        # 返回原 revision（审计实测 Replace 后序列 [7,7,7,7,7]），旧 AI
        # plan 的 base_revision 不失效。undo 恢复内容但 revision 不回退。
        new_project = replace(new_project, revision=int(project.revision) + 1)
        self._undo_stack.append(stack_command)
        self._redo_stack.clear()

        if len(self._undo_stack) > self._max_size:
            self._undo_stack = self._undo_stack[-self._max_size:]

        logger.debug(
            f"ProjectCommandStack: executed {command.label}, "
            f"undo_stack size: {len(self._undo_stack)}"
        )
        return new_project

    def undo(self, project: EditProject) -> EditProject | None:
        """
        Undo the last command.

        Args:
            project: Current project

        Returns:
            New project after undo, or None if nothing to undo
        """
        if not self._undo_stack:
            return None

        self._validate_sequence_payload(self._undo_stack[-1], undo=True)
        command = self._undo_stack.pop()
        # AUD-16204: undo 抛异常时回填命令再上抛（与 CommandStack.undo 一致）。
        try:
            result = command.undo(project)
        except Exception:
            self._undo_stack.append(command)
            raise
        # BE-01：undo 分支同经 consume_project_result 归一（undo 也可能
        # 返回 ProjectTransactionResult/二元组）。
        new_project, _ = consume_project_result(command, result)
        if isinstance(result, ProjectTransactionResult):
            self.last_sequence_updates = dict(result.sequence_map)
        else:
            self.last_sequence_updates = {}
        new_project = replace(new_project, revision=int(project.revision) + 1)
        self._redo_stack.append(command)

        logger.debug(
            f"ProjectCommandStack: undone {command.label}, "
            f"redo_stack size: {len(self._redo_stack)}"
        )
        return new_project

    def redo(self, project: EditProject) -> EditProject | None:
        """
        Redo the last undone command.

        Args:
            project: Current project

        Returns:
            New project after redo, or None if nothing to redo
        """
        if not self._redo_stack:
            return None

        self._validate_sequence_payload(self._redo_stack[-1])
        command = self._redo_stack.pop()
        # AUD-16204（redo 侧同型）：apply 抛异常时回填命令再上抛。
        try:
            result = command.apply(project)
        except Exception:
            self._redo_stack.append(command)
            raise
        # BE-01：redo 分支同经 consume_project_result 归一——redo 重新
        # apply 返回的 updated_command（含新捕获快照）覆盖入栈。
        new_project, effective_command = consume_project_result(
            command, result)
        if isinstance(result, ProjectTransactionResult):
            self.last_sequence_updates = dict(result.sequence_map)
        else:
            self.last_sequence_updates = {}
        new_project = replace(new_project, revision=int(project.revision) + 1)
        self._undo_stack.append(effective_command)

        logger.debug(
            f"ProjectCommandStack: redone {command.label}, "
            f"undo_stack size: {len(self._undo_stack)}"
        )
        return new_project

    def can_undo(self) -> bool:
        """Check if undo is available."""
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        """Check if redo is available."""
        return len(self._redo_stack) > 0

    def get_undo_label(self) -> str | None:
        """Get label of next undoable command."""
        if self._undo_stack:
            return self._undo_stack[-1].label
        return None

    def get_redo_label(self) -> str | None:
        """Get label of next redoable command."""
        if self._redo_stack:
            return self._redo_stack[-1].label
        return None

    def clear(self) -> None:
        """Clear all history."""
        self._undo_stack.clear()
        self._redo_stack.clear()

    def get_history_size(self) -> int:
        """Get total history size."""
        return len(self._undo_stack) + len(self._redo_stack)


# ================================================================
# ProjectMacroCommand - Composite Project-level Operation
# ================================================================

@dataclass(frozen=True)
class ProjectMacroCommand:
    """
    Combines multiple project-level commands into a single undo/redo unit.

    Used by batch operations like ``Delete`` key on the media pool, which may
    delete a mixed selection of assets, timelines, and bins at once.

    Mirrors ``MacroCommand`` but operates on ``EditProject`` rather than
    ``TimelineSequence``. Sub-commands may return ``(new_project,
    updated_command)`` tuples; the macro collects the updated sub-commands and
    returns its own updated copy so snapshots captured during ``apply`` are
    preserved for ``undo``.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        sub_commands: Tuple of project-level sub-commands to apply in order
    """

    command_id: str
    label: str
    sub_commands: tuple[Any, ...]

    def apply(self, project: EditProject) -> EditProject:
        """
        Apply all sub-commands in order.

        Sub-commands may return ``ProjectTransactionResult`` or
        ``(new_project, updated_command)`` tuples; the macro threads the
        project and **persists the updated sub-commands on itself** via
        ``object.__setattr__`` (frozen-dataclass self-update, 与
        scene_cut_controller 等既有先例一致)。BE-01/BE-04：子命令在
        apply 期捕获快照（如 ActivateColorVersion 的 before/after 栈），
        丢弃 updated_command 会让宏 undo 拿不到激活前内容——旧实现直接
        透传裸返回值，遇结构化结果把 Result 对象喂给下一个子命令。

        Args:
            project: Current project

        Returns:
            New project after all sub-commands applied
        """
        current_project = project
        updated_sub_commands: list[Any] = []
        try:
            for cmd in self.sub_commands:
                result = cmd.apply(current_project)
                current_project, effective = consume_project_result(
                    cmd, result)
                updated_sub_commands.append(effective)
        except Exception:
            # 宏是单步用户操作：后续子命令失败时先逆序回滚已应用子命令
            # 再上抛（与时间线 MacroCommand 同型），不留半提交。回滚途中
            # 单个子命令 undo 再抛不中断其余回滚（审查 S7）——收集后
            # 优先上抛原始异常。
            rollback_errors: list[BaseException] = []
            for applied_cmd in reversed(updated_sub_commands):
                try:
                    undo_result = applied_cmd.undo(current_project)
                    current_project, _ = consume_project_result(
                        applied_cmd, undo_result)
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(rollback_exc)
                    logger.exception(
                        "ProjectMacroCommand rollback step failed for %s",
                        getattr(applied_cmd, "label", applied_cmd))
            if rollback_errors:
                logger.error(
                    "ProjectMacroCommand %s rollback incomplete: %d step(s) "
                    "failed", self.label, len(rollback_errors))
            raise
        object.__setattr__(
            self, "sub_commands", tuple(updated_sub_commands))

        logger.debug(
            f"ProjectMacroCommand applied: {self.label} "
            f"({len(self.sub_commands)} sub-commands)"
        )
        return current_project

    def undo(self, project: EditProject) -> EditProject:
        """
        Undo all sub-commands in reverse order.

        Args:
            project: Current project

        Returns:
            Project restored to its pre-apply state
        """
        current_project = project
        for cmd in reversed(self.sub_commands):
            result = cmd.undo(current_project)
            current_project, _ = consume_project_result(cmd, result)

        logger.debug(
            f"ProjectMacroCommand undone: {self.label} "
            f"({len(self.sub_commands)} sub-commands)"
        )
        return current_project

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        sub_cmd_dicts: list[dict[str, Any]] = []
        for cmd in self.sub_commands:
            if hasattr(cmd, "to_dict"):
                sub_cmd_dicts.append(cmd.to_dict())
        return {
            "command_id": self.command_id,
            "command_type": "ProjectMacroCommand",
            "label": self.label,
            "sub_commands": sub_cmd_dicts,
        }

    @staticmethod
    def create(label: str, commands: list[Any]) -> ProjectMacroCommand:
        """Factory method to create macro command."""
        return ProjectMacroCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            sub_commands=tuple(commands),
        )


# ================================================================
# Protocol Implementation Check
# ================================================================

# Note: Frozen dataclass fields appear as descriptors on the class,
# which can cause hasattr to fail unexpectedly. Protocol compliance
# is enforced by static type checkers (mypy/pyright), so runtime
# verification is not strictly necessary here.

# The EditCommand Protocol defines the interface contract. All command
# classes defined in this module implement:
# - command_id: str field
# - label: str field
# - apply(timeline) -> TimelineSequence method
# - undo(timeline) -> TimelineSequence method
# - to_dict() -> dict method


# MacroCommand - Composite Command for Multi-Step Operations
# ================================================================

@dataclass(frozen=True)
class MacroCommand:
    """
    Command that combines multiple atomic commands into a single operation.

    Used for operations like Ripple Delete that need to delete a clip
    and move subsequent clips. All sub-commands are executed/undone together.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        sub_commands: Tuple of sub-commands to execute in order
    """

    command_id: str
    label: str
    sub_commands: tuple[EditCommand, ...]

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply all sub-commands in order.

        Args:
            timeline: Current timeline

        Returns:
            Timeline after all commands applied, with updated command tuple
        """
        current_timeline = timeline
        updated_sub_commands = []
        # R04：宏内顺序 apply 的中间态会把瞬时 gap 当分离，重锚删除
        # 最终仍相接的合法转场（审计实测整体 Move 后转场 1→0）。先快
        # 照各轨转场，全部子命令完成后把"引用片段最终仍相接"的转场从
        # 快照复活并重锚一次。
        transitions_before = {
            track.track_id: tuple(track.transitions)
            for track in timeline.all_tracks
        }
        # N03/C3-03：宏前片段集合——推导"被宏删除"的 owner（复活门）
        clip_ids_before = {
            clip.clip_id for track in timeline.all_tracks
            for clip in track.clips
        }
        try:
            for cmd in self.sub_commands:
                result = cmd.apply(current_timeline)
                # Handle commands that return tuple (timeline, updated_command)
                if isinstance(result, tuple):
                    current_timeline = result[0]
                    updated_sub_commands.append(result[1])
                else:
                    current_timeline = result
                    updated_sub_commands.append(cmd)
        except Exception:
            # A macro is one user-visible operation.  If a later sub-command
            # fails, restore every earlier sub-command before propagating the
            # error so linked A/V edits cannot be left half-applied.
            for applied_cmd in reversed(updated_sub_commands):
                current_timeline = applied_cmd.undo(current_timeline)
            raise

        current_timeline = MacroCommand._resurrect_adjacent_transitions(
            current_timeline, transitions_before,
            sub_commands=tuple(updated_sub_commands),
            clip_ids_before=clip_ids_before,
        )

        # When both members of an existing pair are split together, preserve
        # the link on the right-hand segments as well.  The left segments keep
        # their original IDs, while each right segment gets a fresh reciprocal
        # pair.  This covers Shift-split, scene-cut and other split macros
        # without changing the public SplitClipCommand API.
        split_commands = {
            (cmd.clip_id, cmd.split_frame): cmd
            for cmd in updated_sub_commands
            if isinstance(cmd, SplitClipCommand)
            and cmd._right_clip_id is not None
        }
        for (original_id, split_frame), split_cmd in split_commands.items():
            original = timeline.get_clip_by_id(original_id)
            partner = timeline.get_reciprocal_linked_clip(original)
            if partner is None:
                continue
            partner_cmd = split_commands.get((partner.clip_id, split_frame))
            if partner_cmd is None or partner_cmd._right_clip_id is None:
                continue
            right = current_timeline.get_clip_by_id(split_cmd._right_clip_id)
            partner_right = current_timeline.get_clip_by_id(partner_cmd._right_clip_id)
            if right is None or partner_right is None:
                continue
            current_timeline = current_timeline.with_clip_updated(
                right.with_linked_clip_id(partner_right.clip_id)
            )
            current_timeline = current_timeline.with_clip_updated(
                partner_right.with_linked_clip_id(right.clip_id)
            )

        # Paired Source Overwrite produces new residual IDs on both tracks.
        # Restore the original relationship between corresponding surviving
        # windows; linking only the newly inserted source pair loses the
        # original right-hand A/V association.
        overwrite_descendants: dict[str, list[ClipInstance]] = {}
        for command in updated_sub_commands:
            if not isinstance(command, OverwriteClipCommand):
                continue
            for original in command._original_clips or ():
                ids = {original.clip_id, *(command._split_clip_ids or ())}
                overwrite_descendants[original.clip_id] = [
                    candidate for cid in ids
                    if (candidate := current_timeline.get_clip_by_id(cid)) is not None
                    and candidate.track_id == original.track_id
                    and candidate.asset_id == original.asset_id
                    and original.timeline_start <= candidate.timeline_start
                    and candidate.timeline_end <= original.timeline_end
                ]
        explicitly_unlinked = {clip_id
            for command in updated_sub_commands
            if isinstance(command, SetClipLinkedCommand) and not command.link
            for clip_id in (command.clip_a_id, command.clip_b_id)}
        for original_id, descendants in overwrite_descendants.items():
            if original_id in explicitly_unlinked:
                continue
            original = timeline.get_clip_by_id(original_id)
            partner = timeline.get_reciprocal_linked_clip(original) if original else None
            if (partner is None or partner.clip_id not in overwrite_descendants
                    or partner.clip_id in explicitly_unlinked):
                continue
            for descendant in descendants:
                matches = [candidate for candidate in overwrite_descendants[partner.clip_id]
                           if (candidate.timeline_start == descendant.timeline_start
                               or candidate.timeline_end == descendant.timeline_end)]
                if len(matches) != 1:
                    continue
                other = matches[0]
                current_descendant = current_timeline.get_clip_by_id(descendant.clip_id)
                current_other = current_timeline.get_clip_by_id(other.clip_id)
                if (current_descendant.linked_clip_id not in (None, partner.clip_id, other.clip_id)
                        or current_other.linked_clip_id not in (None, original_id, descendant.clip_id)):
                    continue
                current_timeline = current_timeline.with_clip_updated(
                    current_timeline.get_clip_by_id(descendant.clip_id).with_linked_clip_id(other.clip_id))
                current_timeline = current_timeline.with_clip_updated(
                    current_timeline.get_clip_by_id(other.clip_id).with_linked_clip_id(descendant.clip_id))

        logger.debug(
            f"MacroCommand applied: {self.label} "
            f"({len(self.sub_commands)} sub-commands)"
        )

        # Return updated macro with stored sub-commands for proper undo
        updated_macro = MacroCommand(
            command_id=self.command_id,
            label=self.label,
            sub_commands=tuple(updated_sub_commands),
        )
        return current_timeline, updated_macro

    @staticmethod
    def _collect_explicit_transition_removals(sub_commands) -> set:
        """N03/C3-03：收集宏内（含嵌套宏）显式删除的转场 ID。

        RemoveTransitionCommand 是用户删除意图，最终态必须保持删除；
        复活逻辑不得覆盖它。
        """
        removed: set = set()
        for cmd in sub_commands:
            transition_id = getattr(cmd, "transition_id", None)
            if transition_id is not None and type(cmd).__name__ in (
                "RemoveTransitionCommand",
            ):
                removed.add(str(transition_id))
            nested = getattr(cmd, "sub_commands", None)
            if nested:
                removed |= MacroCommand._collect_explicit_transition_removals(
                    nested)
        return removed

    @staticmethod
    def _resurrect_adjacent_transitions(timeline, transitions_before,
                                         sub_commands=(),
                                         clip_ids_before=None):
        """R04：恢复宏中间态误删、最终几何仍合法的转场。

        N03/C3-03：复活必须尊重删除意图——
        - 宏内显式 RemoveTransitionCommand 删除的转场不复活（否则用户
          删除被宏静默撤销）；
        - left/right 任一 owner 片段在宏中被删除（宏前存在、宏后不存
          在）的转场不复活（否则产生引用已删片段的孤儿转场）。此前
          "任一 owner 还在就复活" 会把 DeleteClip 后应消失的转场复
          活成孤儿引用。
        只有"双 owner 都存活、最终仍相接、且未被显式删除"的转场才可
        复活（整体平移等中间态 gap 误删）。
        """
        explicitly_removed = (
            MacroCommand._collect_explicit_transition_removals(sub_commands)
        )
        ids_before = clip_ids_before or set()
        for track in timeline.all_tracks:
            before = transitions_before.get(track.track_id)
            if not before:
                continue
            current_ids = {t.transition_id for t in track.transitions}
            by_id = {c.clip_id: c for c in track.clips}
            restored = []
            for trans in before:
                if trans.transition_id in current_ids:
                    continue
                if str(trans.transition_id) in explicitly_removed:
                    continue  # 显式删除：保持删除
                left = by_id.get(trans.left_clip_id) if trans.left_clip_id else None
                right = by_id.get(trans.right_clip_id) if trans.right_clip_id else None
                if left is None and right is None:
                    continue
                # owner 被宏删除 → 转场必须随之消失
                if (trans.left_clip_id and trans.left_clip_id in ids_before
                        and left is None):
                    continue
                if (trans.right_clip_id and trans.right_clip_id in ids_before
                        and right is None):
                    continue
                if left is not None and right is not None:
                    if left.timeline_end != right.timeline_start:
                        continue  # 最终仍分离：用户语义删除，保持删除
                restored.append(trans)
            if restored:
                # 显式传 transitions 的 _rebuild 是精确快照语义（不重锚），
                # 复活后重锚一次把 center/duration 对齐最终边缘
                combined_track = track._rebuild(
                    transitions=tuple(track.transitions) + tuple(restored)
                )
                timeline = timeline.with_track_updated(
                    combined_track.reanchor_transitions()
                )
        return timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo all sub-commands in reverse order.

        Args:
            timeline: Current timeline

        Returns:
            Timeline restored to original state
        """
        current_timeline = timeline
        # Undo in reverse order to restore original state correctly
        for cmd in reversed(self.sub_commands):
            current_timeline = cmd.undo(current_timeline)

        logger.debug(
            f"MacroCommand undone: {self.label} "
            f"({len(self.sub_commands)} sub-commands)"
        )
        return current_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        sub_cmd_dicts = []
        for cmd in self.sub_commands:
            if hasattr(cmd, 'to_dict'):
                sub_cmd_dicts.append(cmd.to_dict())

        return {
            "command_id": self.command_id,
            "command_type": "MacroCommand",
            "label": self.label,
            "sub_commands": sub_cmd_dicts,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MacroCommand:
        """Deserialize command.

        AUD-16210: 子命令经 ``_MACRO_SUBCOMMAND_TYPES`` 全量注册表分发
        （本文件每个命令的 ``command_type`` 字符串与类名一致）。未知/
        缺失 command_type 显式抛 ValueError——旧实现只识别 3 种子命令、
        其余静默丢弃，畸形/部分历史反序列化后的宏 redo/undo 只执行子集，
        造成无声的数据错乱；宁可拒载也不允许残缺宏进入执行。
        """
        sub_commands = []
        skipped_types: list[str] = []
        for sub_data in (data.get("sub_commands") or []):
            cmd_type = str(sub_data.get("command_type", "") or "")
            from_dict = _MACRO_SUBCOMMAND_TYPES.get(cmd_type)
            if from_dict is None:
                skipped_types.append(cmd_type or "<missing command_type>")
                continue
            sub_commands.append(from_dict(sub_data))
        if skipped_types:
            raise ValueError(
                f"MacroCommand.from_dict: {len(skipped_types)} sub-command(s) "
                f"have unknown command_type {skipped_types!r} (macro "
                f"{data.get('command_id')!r}); refusing to load a partial macro"
            )

        return MacroCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Macro Command"),
            sub_commands=tuple(sub_commands),
        )

    @staticmethod
    def create(label: str, commands: list[EditCommand]) -> MacroCommand:
        """Factory method to create macro command."""
        return MacroCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            sub_commands=tuple(commands),
        )


# ================================================================
# AUD-16210: MacroCommand 子命令全量反序列化注册表
# ================================================================
#
# command_type 字符串与类名一一对应（to_dict 的既定约定）。仅登记
# apply 作用于 TimelineSequence 的时间线命令——EditProject 级命令
# （Bin/Asset/SmartBin/ColorStack 等）由 ProjectMacroCommand 编组，
# 混入时间线宏只会在 apply 阶段以错误类型崩溃，应在反序列化即拒载
# （MacroCommand.from_dict 对未登记类型抛 ValueError，禁止静默丢弃
# 子命令导致宏残缺）。新增时间线命令后请同步登记；
# tests/edit/test_fix_wd9g55_aud.py 的完备性测试会在遗漏时失败。

from src.features.edit.core.timeline_settings_command import TimelineSettingsCommand
from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand


# P6 拆分·第一刀：audio/automation 簇（18 类）已纯搬移至 audio_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .audio_commands import (
    AddAutomationKeyframeCommand,
    RemoveAutomationKeyframeCommand,
    ClearAutomationCommand,
    SetAutomationKeyframeInterpolationCommand,
    SetTrackVolumeCommand,
    SetTrackPanCommand,
    SetTrackStripEQCommand,
    SetTrackEffectChainCommand,
    SetMasterVolumeCommand,
    SetMasterMuteCommand,
    ApplyChannelStripPresetCommand,
    ApplyMixSnapshotCommand,
    AddBusCommand,
    RemoveBusCommand,
    SetTrackOutputBusCommand,
    AddSendCommand,
    RemoveSendCommand,
    UpdateSendCommand,
)


# P6 拆分·第二刀：color 簇（5 类）已纯搬移至 color_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .color_commands import (
    CreateColorStackCommand as CreateColorStackCommand,
    UpdateColorStackCommand as UpdateColorStackCommand,
    UpdateClipColorStackCommand,
    UseSharedGradeCommand,
    MakeLocalGradeCopyCommand,
)


# P6 拆分·第三刀：transitions 簇（3 类）已纯搬移至 transition_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .transition_commands import (
    AddTransitionCommand,
    RemoveTransitionCommand,
    UpdateTransitionCommand,
)


# P6 拆分·第三刀：markers 簇（4 类）已纯搬移至 marker_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .marker_commands import (
    AddMarkerCommand,
    DeleteMarkerCommand,
    MoveMarkerCommand,
    EditMarkerCommand,
)


# P6 拆分·第四刀：subtitle 簇（6 类）已纯搬移至 subtitle_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .subtitle_commands import (
    CaptionEditMode as CaptionEditMode,
    AddSubtitleClipCommand,
    UpdateSubtitleClipCommand,
    MoveSubtitleClipCommand,
    CopySubtitleClipCommand,
    DeleteSubtitleClipCommand,
)


# P6 拆分·第四刀：track 簇（10 类）已纯搬移至 track_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .track_commands import (
    SetTrackMuteCommand,
    SetTrackSoloCommand,
    SetTrackLockCommand,
    SetTrackSyncLockCommand,
    SetTrackVisibilityCommand,
    AddTrackCommand,
    DeleteTrackCommand,
    RenameTrackCommand,
    SetTrackColorCommand,
    MoveTrackCommand,
)


# P6 拆分·第五刀：asset 簇（29 类）已纯搬移至 asset_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .asset_commands import (
    CreateBinCommand as CreateBinCommand,
    DeleteBinCommand as DeleteBinCommand,
    DeleteAssetCommand as DeleteAssetCommand,
    DeleteTimelineCommand as DeleteTimelineCommand,
    RenameBinCommand as RenameBinCommand,
    RenameAssetCommand as RenameAssetCommand,
    MoveBinCommand as MoveBinCommand,
    MoveAssetToBinCommand as MoveAssetToBinCommand,
    MoveTimelineToBinCommand as MoveTimelineToBinCommand,
    MoveAssetBetweenBinsCommand as MoveAssetBetweenBinsCommand,
    MoveTimelineBetweenBinsCommand as MoveTimelineBetweenBinsCommand,
    UnfileAssetCommand as UnfileAssetCommand,
    UnfileTimelineCommand as UnfileTimelineCommand,
    RemoveAssetFromBinCommand as RemoveAssetFromBinCommand,
    RemoveTimelineFromBinCommand as RemoveTimelineFromBinCommand,
    DuplicateAssetCommand as DuplicateAssetCommand,
    UpdateAssetProductionInfoCommand as UpdateAssetProductionInfoCommand,
    UpdateProxyPolicyCommand as UpdateProxyPolicyCommand,
    UpdateAssetLogCommand as UpdateAssetLogCommand,
    SetAssetInOutCommand as SetAssetInOutCommand,
    SetAssetMarkersCommand as SetAssetMarkersCommand,
    CreateSubclipCommand as CreateSubclipCommand,
    UpdateSourcePatchCommand as UpdateSourcePatchCommand,
    UpdateAssetInterpretationCommand as UpdateAssetInterpretationCommand,
    UpdateAudioSyncBindingCommand as UpdateAudioSyncBindingCommand,
    UpdateClipFpsInterpretationCommand,
    BatchUpdateAssetLogCommand as BatchUpdateAssetLogCommand,
    CreateSmartBinCommand as CreateSmartBinCommand,
    UpdateSmartBinRulesCommand as UpdateSmartBinRulesCommand,
    ImportMetadataCommand as ImportMetadataCommand,
    DeleteSmartBinCommand as DeleteSmartBinCommand,
    ReplaceMediaCommand as ReplaceMediaCommand,
    RelinkAssetCommand as RelinkAssetCommand,
    BatchRelinkAssetsCommand as BatchRelinkAssetsCommand,
    UpdateAssetProxyCommand as UpdateAssetProxyCommand,
    ImportMediaAssetCommand as ImportMediaAssetCommand,
    MarkBestTakeCommand as MarkBestTakeCommand,
    UpdateTimelineSettingsCommand as UpdateTimelineSettingsCommand,
)

# P6 拆分·第五刀：双簇共享符号（assert_track_editable＝轨锁守卫、
# ProjectTransactionResult＝项目事务结果协议）下沉 _support.py，此处
# re-import 保住包级深导入（edit_controller 直接 import assert_track_editable）。
# P6 拆分·第七刀：快照反序列化 _clips_from_dicts/_transitions_from_dicts 同为
# 真共享符号（clip 簇与留守 RippleDelete/BatchDelete/InsertLinkedAssetRipple
# 的 from_dict 共用），随第七刀下沉 _support 并在此 re-import。
from ._support import (
    ProjectTransactionResult,
    assert_track_editable as assert_track_editable,
    consume_project_result as consume_project_result,
    _clips_from_dicts as _clips_from_dicts,
    _transitions_from_dicts as _transitions_from_dicts,
)


# P6 拆分·第六刀：params_keyfx 簇（10 类）已纯搬移至 effect_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
from .effect_commands import (
    UpdateClipParamCommand,
    AddKeyframeCommand,
    RemoveKeyframeCommand,
    MoveKeyframeCommand,
    UpdateKeyframeCommand,
    SetKeyframeInterpolationCommand,
    AddEffectCommand,
    RemoveEffectCommand,
    UpdateEffectParamCommand,
    SetEffectEnabledCommand,
    ReorderEffectCommand,
)


# P6 拆分·第七刀：clip 基础操作簇（16 类）已纯搬移至 clip_commands.py，
# 此处显式 re-export 保住 `from src.features.edit.core.commands import X` 深导入。
# 留守 MacroCommand 对 SplitClip/OverwriteClip/SetClipLinked 的 isinstance 及
# RippleDelete/BatchDelete→MoveClip、SwitchMulticamAngle→SplitClip 的引用均在
# 方法体内（调用期名字解析）；本块位于 _MACRO_SUBCOMMAND_TYPES 之前，re-export
# 名先于任何运行期查找完成绑定。
from .clip_commands import (
    AddClipCommand,
    DeleteClipCommand,
    MoveClipCommand,
    MoveClipToTrackCommand,
    CopyClipsToNewTrackCommand,
    MoveClipToExistingTrackCommand,
    MoveClipsOverwriteCommand as MoveClipsOverwriteCommand,
    TrimClipCommand,
    SlipClipCommand,
    SplitClipCommand,
    SlideClipCommand,
    InsertClipCommand,
    OverwriteClipCommand,
    RollTrimCommand,
    RippleTrimCommand,
    SetClipLinkedCommand,
)


# P6 拆分·第八刀：clip_props 簇（14 类）→ clip_props_commands.py、
# insert_ripple 余部（6 类）→ ripple_commands.py，均已纯搬移，此处显式
# re-export 保住 `from src.features.edit.core.commands import X` 深导入。
# _MACRO_SUBCOMMAND_TYPES 引用其中 18 类（ChangeClipSpeed 与
# RangeRippleDelete 不在字典内，按先例以 as 形式 re-export）。
from .clip_props_commands import (
    RetimeMotionGraphicClipCommand,
    UpdateClipSourceCommand,
    SwitchMulticamAngleCommand,
    ChangeClipFadeCommand,
    SetClipLabelColorCommand,
    SetClipWaveformDisplayCommand,
    SetTimelineRangeCommand,
    ToggleClipEnabledCommand,
    ChangeClipSpeedCommand as ChangeClipSpeedCommand,
    MoveSpeedPointCommand,
    SetClipSpeedSettingsCommand,
    SetFadeCurveCommand,
    SetFadeEndCurveCommand,
    SetClipFreezeCommand,
    FreezeFrameAtPlayheadCommand,
    SetClipBlendModeCommand,
    ReplaceClipAssetCommand,
    ReplaceClipWithSourceCommand,
    SetClipFlagCommand,
    SetClipAudioGainCommand,
    UpdateClipSampleFieldsCommand,
)
from .ripple_commands import (
    InsertLinkedAssetCommand,
    InsertLinkedAssetRippleCommand,
    RippleDeleteCommand,
    RangeRippleDeleteCommand as RangeRippleDeleteCommand,
    BatchDeleteCommand,
    AppendClipCommand,
)


_MACRO_SUBCOMMAND_TYPES: dict[str, Any] = {
    "MoveSpeedPointCommand": MoveSpeedPointCommand.from_dict,
    "SetClipSpeedSettingsCommand": SetClipSpeedSettingsCommand.from_dict,
    "SetFadeCurveCommand": SetFadeCurveCommand.from_dict,
    "SetFadeEndCurveCommand": SetFadeEndCurveCommand.from_dict,
    "SetClipFreezeCommand": SetClipFreezeCommand.from_dict,
    "TimelineProjectTransactionCommand": TimelineProjectTransactionCommand.from_dict,
    "TimelineSettingsCommand": TimelineSettingsCommand.from_dict,
    "AddClipCommand": AddClipCommand.from_dict,
    "DeleteClipCommand": DeleteClipCommand.from_dict,
    "MoveClipCommand": MoveClipCommand.from_dict,
    "MoveClipToTrackCommand": MoveClipToTrackCommand.from_dict,
    "CopyClipsToNewTrackCommand": CopyClipsToNewTrackCommand.from_dict,
    "TrimClipCommand": TrimClipCommand.from_dict,
    "SlipClipCommand": SlipClipCommand.from_dict,
    "SplitClipCommand": SplitClipCommand.from_dict,
    "UpdateClipSourceCommand": UpdateClipSourceCommand.from_dict,
    "UpdateClipFpsInterpretationCommand":
        UpdateClipFpsInterpretationCommand.from_dict,
    "UpdateClipParamCommand": UpdateClipParamCommand.from_dict,
    "AddKeyframeCommand": AddKeyframeCommand.from_dict,
    "RemoveKeyframeCommand": RemoveKeyframeCommand.from_dict,
    "MoveKeyframeCommand": MoveKeyframeCommand.from_dict,
    "UpdateKeyframeCommand": UpdateKeyframeCommand.from_dict,
    "SetKeyframeInterpolationCommand": SetKeyframeInterpolationCommand.from_dict,
    "SetAutomationKeyframeInterpolationCommand":
        SetAutomationKeyframeInterpolationCommand.from_dict,
    "SetTrackMuteCommand": SetTrackMuteCommand.from_dict,
    "SetTrackSoloCommand": SetTrackSoloCommand.from_dict,
    "SetTrackLockCommand": SetTrackLockCommand.from_dict,
    "SetTrackSyncLockCommand": SetTrackSyncLockCommand.from_dict,
    "SetTrackVolumeCommand": SetTrackVolumeCommand.from_dict,
    "SetTrackPanCommand": SetTrackPanCommand.from_dict,
    "SetTrackStripEQCommand": SetTrackStripEQCommand.from_dict,
    "SetTrackEffectChainCommand": SetTrackEffectChainCommand.from_dict,
    "SetMasterVolumeCommand": SetMasterVolumeCommand.from_dict,
    "SetMasterMuteCommand": SetMasterMuteCommand.from_dict,
    "ApplyChannelStripPresetCommand": ApplyChannelStripPresetCommand.from_dict,
    "ApplyMixSnapshotCommand": ApplyMixSnapshotCommand.from_dict,
    "SetTrackVisibilityCommand": SetTrackVisibilityCommand.from_dict,
    "AddTrackCommand": AddTrackCommand.from_dict,
    "AddSubtitleClipCommand": AddSubtitleClipCommand.from_dict,
    "UpdateSubtitleClipCommand": UpdateSubtitleClipCommand.from_dict,
    "MoveSubtitleClipCommand": MoveSubtitleClipCommand.from_dict,
    "CopySubtitleClipCommand": CopySubtitleClipCommand.from_dict,
    "DeleteSubtitleClipCommand": DeleteSubtitleClipCommand.from_dict,
    "DeleteTrackCommand": DeleteTrackCommand.from_dict,
    "RenameTrackCommand": RenameTrackCommand.from_dict,
    "SetTrackColorCommand": SetTrackColorCommand.from_dict,
    "AddAutomationKeyframeCommand": AddAutomationKeyframeCommand.from_dict,
    "RemoveAutomationKeyframeCommand": RemoveAutomationKeyframeCommand.from_dict,
    "ClearAutomationCommand": ClearAutomationCommand.from_dict,
    "AddBusCommand": AddBusCommand.from_dict,
    "RemoveBusCommand": RemoveBusCommand.from_dict,
    "SetTrackOutputBusCommand": SetTrackOutputBusCommand.from_dict,
    "AddSendCommand": AddSendCommand.from_dict,
    "RemoveSendCommand": RemoveSendCommand.from_dict,
    "UpdateSendCommand": UpdateSendCommand.from_dict,
    "MoveTrackCommand": MoveTrackCommand.from_dict,
    "InsertLinkedAssetCommand": InsertLinkedAssetCommand.from_dict,
    "InsertLinkedAssetRippleCommand": InsertLinkedAssetRippleCommand.from_dict,
    "UpdateClipSampleFieldsCommand": UpdateClipSampleFieldsCommand.from_dict,
    "MacroCommand": MacroCommand.from_dict,
    "RippleDeleteCommand": RippleDeleteCommand.from_dict,
    "SlideClipCommand": SlideClipCommand.from_dict,
    "BatchDeleteCommand": BatchDeleteCommand.from_dict,
    "AddTransitionCommand": AddTransitionCommand.from_dict,
    "RemoveTransitionCommand": RemoveTransitionCommand.from_dict,
    "UpdateTransitionCommand": UpdateTransitionCommand.from_dict,
    "InsertClipCommand": InsertClipCommand.from_dict,
    "OverwriteClipCommand": OverwriteClipCommand.from_dict,
    "AppendClipCommand": AppendClipCommand.from_dict,
    "RollTrimCommand": RollTrimCommand.from_dict,
    "RippleTrimCommand": RippleTrimCommand.from_dict,
    "AddEffectCommand": AddEffectCommand.from_dict,
    "RemoveEffectCommand": RemoveEffectCommand.from_dict,
    "UpdateEffectParamCommand": UpdateEffectParamCommand.from_dict,
    "SetEffectEnabledCommand": SetEffectEnabledCommand.from_dict,
    "ReorderEffectCommand": ReorderEffectCommand.from_dict,
    "UpdateClipColorStackCommand": UpdateClipColorStackCommand.from_dict,
    "UseSharedGradeCommand": UseSharedGradeCommand.from_dict,
    "MakeLocalGradeCopyCommand": MakeLocalGradeCopyCommand.from_dict,
    "AddMarkerCommand": AddMarkerCommand.from_dict,
    "DeleteMarkerCommand": DeleteMarkerCommand.from_dict,
    "MoveMarkerCommand": MoveMarkerCommand.from_dict,
    "EditMarkerCommand": EditMarkerCommand.from_dict,
    "ChangeClipFadeCommand": ChangeClipFadeCommand.from_dict,
    "FreezeFrameAtPlayheadCommand": FreezeFrameAtPlayheadCommand.from_dict,
    "SetClipBlendModeCommand": SetClipBlendModeCommand.from_dict,
    "ReplaceClipAssetCommand": ReplaceClipAssetCommand.from_dict,
    "ReplaceClipWithSourceCommand": ReplaceClipWithSourceCommand.from_dict,
    "SetClipFlagCommand": SetClipFlagCommand.from_dict,
    "SetClipAudioGainCommand": SetClipAudioGainCommand.from_dict,
    "MoveClipToExistingTrackCommand": MoveClipToExistingTrackCommand.from_dict,
    "RetimeMotionGraphicClipCommand": RetimeMotionGraphicClipCommand.from_dict,
    "SwitchMulticamAngleCommand": SwitchMulticamAngleCommand.from_dict,
    "SetClipLabelColorCommand": SetClipLabelColorCommand.from_dict,
    "SetClipWaveformDisplayCommand": SetClipWaveformDisplayCommand.from_dict,
    "SetTimelineRangeCommand": SetTimelineRangeCommand.from_dict,
    "ToggleClipEnabledCommand": ToggleClipEnabledCommand.from_dict,
    "SetClipLinkedCommand": SetClipLinkedCommand.from_dict,
}
