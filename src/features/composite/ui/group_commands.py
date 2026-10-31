"""
Group Commands - Undo/redo commands for group/macro operations.

Provides commands for:
- Creating groups from selected nodes (simple UI grouping)
- Ungrouping (dissolving) groups
- Adding/removing nodes from groups
- Entering groups for editing (expand/collapse)

Usage:
    from .group_commands import CreateGroupCommand, UngroupCommand, EnterGroupCommand

    # Create a group from selected nodes
    cmd = CreateGroupCommand(graph, selected_node_ids, group_id, group_data)
    undo_stack.push(cmd)

AUD-9920: undo integration is owned by CompositePage. The editor applies a
group command and then emits ``group_commands_applied``; the page wraps the
applied command objects into a single ``UndoAction`` on its UndoStack.
``build_group_command_from_selection`` returns the command unapplied for
callers that need to keep it for the undo record.
"""

from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from dataclasses import dataclass
import copy
import uuid

if TYPE_CHECKING:
    from src.features.composite.core.node_engine import NodeGraph


@dataclass
class CreateGroupCommand:
    """Command to create a group from selected nodes.
    
    This is a UI-level grouping that does not affect rendering.
    The group is stored in graph._node_groups as a simple container.
    """
    
    graph: 'NodeGraph'
    node_ids: List[str]
    group_id: str
    group_data: Dict[str, Any]  # Contains title, color, position, etc.
    
    def __post_init__(self) -> None:
        self.description = f"Create group with {len(self.node_ids)} nodes"
    
    def redo(self) -> str:
        """Create the group."""
        if not hasattr(self.graph, '_node_groups'):
            self.graph._node_groups: Dict[str, Dict] = {}
        self.graph._node_groups[self.group_id] = {
            'id': self.group_id,
            'node_ids': list(self.node_ids),
            **self.group_data
        }
        return self.group_id
    
    def undo(self) -> None:
        """Remove the group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            del self.graph._node_groups[self.group_id]


@dataclass
class UngroupCommand:
    """Command to dissolve a group (keeping nodes in the graph)."""
    
    graph: 'NodeGraph'
    group_id: str
    
    def __post_init__(self) -> None:
        self._group_data: Optional[Dict] = None
        self.description = "Ungroup nodes"
    
    def redo(self) -> Optional[Dict]:
        """Dissolve the group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            # AUD-9919: deep-copy the snapshot. dict.copy() is shallow, so
            # ``_group_data['node_ids']`` stayed the same list object as the
            # live group — a later Add/Remove undo mutating the restored
            # group silently rewrote this snapshot, losing members across
            # undo/redo interleavings.
            self._group_data = copy.deepcopy(
                self.graph._node_groups[self.group_id])
            del self.graph._node_groups[self.group_id]
            return self._group_data
        return None

    def undo(self) -> None:
        """Restore the group."""
        if self._group_data and hasattr(self.graph, '_node_groups'):
            # Restore an isolated copy so the live group never aliases the
            # kept snapshot (same AUD-9919 reasoning, opposite direction).
            self.graph._node_groups[self.group_id] = copy.deepcopy(
                self._group_data)


@dataclass
class AddToGroupCommand:
    """Command to add nodes to an existing group."""
    
    graph: 'NodeGraph'
    group_id: str
    node_ids: List[str]
    
    def __post_init__(self) -> None:
        self.description = f"Add {len(self.node_ids)} nodes to group"
    
    def redo(self) -> None:
        """Add nodes to group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            group = self.graph._node_groups[self.group_id]
            # AUD-9919: build a NEW list instead of appending in place —
            # in-place mutation rewrote any snapshot (e.g. an armed
            # UngroupCommand) that still shared this list object.
            merged = list(group.get('node_ids') or [])
            changed = False
            for nid in self.node_ids:
                if nid not in merged:
                    merged.append(nid)
                    changed = True
            if changed:
                group['node_ids'] = merged

    def undo(self) -> None:
        """Remove nodes from group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            group = self.graph._node_groups[self.group_id]
            # AUD-9919: same non-mutating replacement on the way back.
            remaining = list(group.get('node_ids') or [])
            changed = False
            for nid in self.node_ids:
                if nid in remaining:
                    remaining.remove(nid)
                    changed = True
            if changed:
                group['node_ids'] = remaining


@dataclass
class RemoveFromGroupCommand:
    """Command to remove nodes from a group."""

    graph: 'NodeGraph'
    group_id: str
    node_ids: List[str]

    def __post_init__(self) -> None:
        self.description = f"Remove {len(self.node_ids)} nodes from group"

    def redo(self) -> None:
        """Remove nodes from group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            group = self.graph._node_groups[self.group_id]
            # AUD-9919: non-mutating list replacement (see AddToGroupCommand).
            remaining = list(group.get('node_ids') or [])
            changed = False
            for nid in self.node_ids:
                if nid in remaining:
                    remaining.remove(nid)
                    changed = True
            if changed:
                group['node_ids'] = remaining

    def undo(self) -> None:
        """Restore nodes to group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            group = self.graph._node_groups[self.group_id]
            # AUD-9919: non-mutating list replacement (see AddToGroupCommand).
            merged = list(group.get('node_ids') or [])
            changed = False
            for nid in self.node_ids:
                if nid not in merged:
                    merged.append(nid)
                    changed = True
            if changed:
                group['node_ids'] = merged


@dataclass
class UpdateGroupCommand:
    """Command to update group properties (title, color, position, etc.)."""
    
    graph: 'NodeGraph'
    group_id: str
    new_data: Dict[str, Any]
    
    def __post_init__(self) -> None:
        self._old_data: Optional[Dict] = None
        self.description = "Update group"
    
    def redo(self) -> None:
        """Update group properties."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            if self._old_data is None:
                # AUD-9918: deep-copy the pre-update snapshot. The old
                # shallow dict() copy shared the live group object, so the
                # in-place .update() below polluted the snapshot after the
                # first undo — the second undo then "restored" the NEW
                # values and the old properties were lost forever.
                self._old_data = copy.deepcopy(
                    self.graph._node_groups[self.group_id])
            # Replace instead of updating in place so the applied state and
            # the snapshot can never alias the same dict.
            self.graph._node_groups[self.group_id] = {
                **self.graph._node_groups[self.group_id],
                **self.new_data,
            }

    def undo(self) -> None:
        """Restore old group properties."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups and self._old_data:
            # AUD-9918: restore an isolated copy, keeping the snapshot
            # pristine for the next redo/undo round-trip.
            self.graph._node_groups[self.group_id] = copy.deepcopy(
                self._old_data)


@dataclass
class EnterGroupCommand:
    """Command to enter a group for editing (expand it).
    
    Sets the group's is_expanded flag, which the UI uses to show
    the contained nodes in an editable view.
    """
    
    graph: 'NodeGraph'
    group_id: str
    
    def __post_init__(self) -> None:
        self._was_expanded: bool = False
        self.description = "Enter group"
    
    def redo(self) -> bool:
        """Enter (expand) the group."""
        if not hasattr(self.graph, '_node_groups'):
            return False
        if self.group_id not in self.graph._node_groups:
            return False
        
        group = self.graph._node_groups[self.group_id]
        self._was_expanded = group.get('is_expanded', False)
        group['is_expanded'] = True
        return True
    
    def undo(self) -> None:
        """Exit (collapse) the group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            self.graph._node_groups[self.group_id]['is_expanded'] = self._was_expanded


@dataclass
class ExitGroupCommand:
    """Command to exit group editing (collapse it).
    
    Clears the group's is_expanded flag, returning to the parent view.
    """
    
    graph: 'NodeGraph'
    group_id: str
    
    def __post_init__(self) -> None:
        self._was_expanded: bool = True
        self.description = "Exit group"
    
    def redo(self) -> bool:
        """Exit (collapse) the group."""
        if not hasattr(self.graph, '_node_groups'):
            return False
        if self.group_id not in self.graph._node_groups:
            return False
        
        group = self.graph._node_groups[self.group_id]
        self._was_expanded = group.get('is_expanded', True)
        group['is_expanded'] = False
        return True
    
    def undo(self) -> None:
        """Re-enter (expand) the group."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            self.graph._node_groups[self.group_id]['is_expanded'] = self._was_expanded


# ── Helper functions ──

def build_group_command_from_selection(
        graph: 'NodeGraph', node_ids: List[str],
        title: str = "Group", color: str = "#4A90D9",
        position: Optional[Tuple[float, float]] = None) -> 'CreateGroupCommand':
    """Build (but do NOT apply) a CreateGroupCommand for the selection.

    AUD-9920: callers that own an undo record must apply the command
    themselves and keep the object alive for undo. The legacy
    ``create_group_from_selection`` applied internally and returned only the
    id, so the command object (and with it the ability to undo) was dropped.

    Args:
        graph: The NodeGraph instance
        node_ids: List of node IDs to include in the group
        title: Display title for the group
        color: Background/border color for the group
        position: Optional position for the group bounding box

    Returns:
        An unapplied CreateGroupCommand (call ``redo()`` to apply).
    """
    # AUD-9921：8 字符 uuid 片段存在碰撞概率，CreateGroupCommand.redo 按
    # group_id 直接写入 graph._node_groups——撞上已有组会静默覆盖其成员。
    # 碰撞时重生成；8 位片段连续碰撞的极端兜底改用完整 uuid。
    existing_ids = set(getattr(graph, "_node_groups", {}) or {})
    group_id = str(uuid.uuid4())[:8]
    attempts = 0
    while group_id in existing_ids and attempts < 8:
        group_id = str(uuid.uuid4())[:8]
        attempts += 1
    if group_id in existing_ids:
        group_id = str(uuid.uuid4())

    # Calculate bounding box for group position if not provided
    nodes = [graph.get_node(nid) for nid in node_ids if graph.get_node(nid)]
    if nodes and position is None:
        x = min(n.position[0] for n in nodes)
        y = min(n.position[1] for n in nodes)
        position = (x - 20, y - 40)  # Add padding above

    group_data = {
        'title': title,
        'color': color,
        'position': position or (0.0, 0.0),
        'size': (300.0, 200.0),
        'z_order': -10,
        'is_expanded': False,
    }

    return CreateGroupCommand(graph, node_ids, group_id, group_data)


def create_group_from_selection(graph: 'NodeGraph', node_ids: List[str],
                                 title: str = "Group", color: str = "#4A90D9",
                                 position: Optional[Tuple[float, float]] = None) -> str:
    """Helper to create a group from selected nodes (applies immediately).

    Convenience wrapper over ``build_group_command_from_selection`` for
    callers without an undo stack (tests, programmatic graph setup). The
    command is applied here and NOT recorded anywhere — UI flows must use
    the builder and register undo (AUD-9920).

    Args:
        graph: The NodeGraph instance
        node_ids: List of node IDs to include in the group
        title: Display title for the group
        color: Background/border color for the group
        position: Optional position for the group bounding box

    Returns:
        The created group ID
    """
    cmd = build_group_command_from_selection(
        graph, node_ids, title=title, color=color, position=position)
    cmd.redo()
    return cmd.group_id


def get_group_for_node(graph: 'NodeGraph', node_id: str) -> Optional[Dict]:
    """Get the group that contains a node, if any.
    
    Args:
        graph: The NodeGraph instance
        node_id: The node ID to search for
        
    Returns:
        Group data dict if found, None otherwise
    """
    if not hasattr(graph, '_node_groups'):
        return None
    for group in graph._node_groups.values():
        if node_id in group['node_ids']:
            return group
    return None


def get_nodes_in_group(graph: 'NodeGraph', group_id: str) -> List[str]:
    """Get all node IDs in a group.
    
    Args:
        graph: The NodeGraph instance
        group_id: The group ID
        
    Returns:
        List of node IDs in the group
    """
    if not hasattr(graph, '_node_groups') or group_id not in graph._node_groups:
        return []
    return list(graph._node_groups[group_id]['node_ids'])


def get_all_groups(graph: 'NodeGraph') -> Dict[str, Dict]:
    """Get all groups in the graph.
    
    Args:
        graph: The NodeGraph instance
        
    Returns:
        Dict of group_id -> group_data
    """
    if not hasattr(graph, '_node_groups'):
        return {}
    return dict(graph._node_groups)
