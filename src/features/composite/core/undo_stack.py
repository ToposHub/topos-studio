"""
Undo Stack - Enhanced undo/redo support for the node graph editor.

Supports:
- Node add/remove
- Connection/disconnection
- Parameter changes
- Node moves
- Command merging (coalesce consecutive param changes)
- Command compression (auto-compress when exceeding limit)
- Branch undo (tree-based history, switch between branches)
- Persistence (save/load history to disk)

Architecture:
- UndoAction: Single undoable action with merge support
- UndoNode: Node in undo tree (for branch undo)
- UndoTree: Tree-based undo history
- UndoStack: High-level API that wraps UndoTree
"""

import logging
from typing import List, Dict, Any, Optional, Callable, Tuple
from PyQt6.QtCore import QObject, pyqtSignal
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import uuid

logger = logging.getLogger(__name__)


class UndoAction:
    """A single undoable action with merge support."""

    def __init__(self, action_type: str, data: dict,
                 undo_fn: Callable, redo_fn: Callable,
                 description: str = "",
                 merge_key: Optional[str] = None,
                 merge_window_ms: int = 500) -> None:
        """
        Args:
            action_type: Type of action (e.g., "param_change", "move_nodes")
            data: Action data (used by undo/redo functions)
            undo_fn: Function to undo the action (takes data dict)
            redo_fn: Function to redo the action (takes data dict)
            description: Human-readable description
            merge_key: Key for merging (e.g., "param_change:node_id:param_name")
                       If None, action is not mergeable
            merge_window_ms: Time window for merging (milliseconds)
        """
        self.action_type = action_type
        self.data = data
        self.undo_fn = undo_fn
        self.redo_fn = redo_fn
        self.description = description or action_type
        self.merge_key = merge_key
        self.merge_window_ms = merge_window_ms
        self.timestamp = datetime.now()
        self.is_compressed = False  # Flag for compressed actions

    def undo(self) -> None:
        """Execute undo function."""
        self.undo_fn(self.data)

    def redo(self) -> None:
        """Execute redo function."""
        self.redo_fn(self.data)

    def is_mergeable(self) -> bool:
        """Check if this action can be merged with others."""
        return self.merge_key is not None

    def can_merge_with(self, other: 'UndoAction') -> bool:
        """Check if this action can merge with another action.

        Conditions:
        - Both have the same merge_key
        - Both are mergeable
        - Time difference is within merge_window
        - The other action is not already merged (self can be merged result)
        """
        if not self.is_mergeable() or not other.is_mergeable():
            return False
        if self.merge_key != other.merge_key:
            return False
        # Only check other.is_compressed, allow self to be merged result
        if other.is_compressed:
            return False

        # Check time window
        time_diff_ms = abs((self.timestamp - other.timestamp).total_seconds() * 1000)
        return time_diff_ms <= self.merge_window_ms

    def merge_with(self, other: 'UndoAction') -> 'UndoAction':
        """Merge this action with another action.

        Creates a new action with:
        - old value from this action (earlier)
        - new value from other action (later)
        - Combined description

        Note: This action should be the earlier one, other is the later one.
        """
        # For param_change, keep the original old value and the latest new value
        merged_data = self.data.copy()

        # Update new value from other action
        if self.action_type == "param_change":
            merged_data["new"] = other.data.get("new")
            merged_description = f"Change {self.data.get('param', '')}"
        elif self.action_type == "move_nodes":
            # For moves, keep the original old positions and latest new positions
            merged_data["new_positions"] = other.data.get("new_positions", [])
            merged_description = "Move nodes"
        else:
            merged_description = f"Merged {self.action_type}"

        merged = UndoAction(
            self.action_type,
            merged_data,
            self.undo_fn,
            self.redo_fn,
            merged_description,
            self.merge_key,
            self.merge_window_ms,
        )
        merged.timestamp = self.timestamp  # Keep original timestamp
        merged.is_compressed = True  # Mark as merged

        return merged

    def to_dict(self) -> dict:
        """Serialize action for persistence.

        Note: undo_fn and redo_fn are NOT serialized.
        They need to be restored via restore_callbacks().
        """
        data = self.data
        if self.action_type == "group":
            # AUD-5411: group data embeds UndoAction objects in
            # data["actions"].  Serialize them recursively, otherwise
            # json.dump raises TypeError and the WHOLE history file
            # fails to save.
            actions = data.get("actions") if isinstance(data, dict) else None
            if isinstance(actions, (list, tuple)):
                data = dict(data)
                data["actions"] = [
                    a.to_dict() if isinstance(a, UndoAction) else a
                    for a in actions
                ]
        return {
            "action_type": self.action_type,
            "data": data,
            "description": self.description,
            "merge_key": self.merge_key,
            "merge_window_ms": self.merge_window_ms,
            "timestamp": self.timestamp.isoformat(),
            "is_compressed": self.is_compressed,
        }

    @classmethod
    def from_dict(cls, data: dict, undo_fn: Callable, redo_fn: Callable) -> 'UndoAction':
        """Deserialize action from dict.

        Args:
            data: Serialized action data
            undo_fn: Restored undo function
            redo_fn: Restored redo function
        """
        action = cls(
            data["action_type"],
            data["data"],
            undo_fn,
            redo_fn,
            data.get("description", ""),
            data.get("merge_key"),
            data.get("merge_window_ms", 500),
        )
        action.timestamp = datetime.fromisoformat(data["timestamp"])
        action.is_compressed = data.get("is_compressed", False)
        return action


@dataclass
class UndoNode:
    """A node in the undo tree (for branch undo).

    Each node represents a state in history.
    Children are branches from this state.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    action: Optional[UndoAction] = None
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    description: str = ""
    branch_name: str = "main"  # Branch identifier
    # AUD-5410: transient restoration payload. from_dict parks the
    # serialized action dict here; UndoStack.load_from_file converts it
    # into a real UndoAction (needs the callback registry) and clears it.
    # Never serialized — to_dict() re-serializes ``action`` instead.
    pending_action: Optional[dict] = None

    def is_root(self) -> bool:
        return self.parent_id is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action.to_dict() if self.action else None,
            "parent_id": self.parent_id,
            "children": self.children,
            "timestamp": self.timestamp.isoformat(),
            "description": self.description,
            "branch_name": self.branch_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'UndoNode':
        node = cls(
            id=data["id"],
            parent_id=data.get("parent_id"),
            children=data.get("children", []),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            description=data.get("description", ""),
            branch_name=data.get("branch_name", "main"),
        )
        # AUD-5410: keep the serialized action payload so load_from_file can
        # restore it through the callback registry. The previous code
        # dropped the "action" key entirely, which made the restoration
        # loop below dead code (node.action was always None).
        node.pending_action = data.get("action")
        return node


class UndoTree:
    """Tree-based undo history for branch undo support.

    Architecture:
    - Root node represents the initial state (no action)
    - Each child is a new state after an action
    - Multiple children = multiple branches
    - Current position is tracked by current_node_id
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, UndoNode] = {}
        self._current_node_id: Optional[str] = None
        self._root_node_id: str = ""

        # Branch selection: tracks the preferred branch for redo operations
        # When multiple branches exist at a node, redo will prefer this branch
        self._selected_branch: Optional[str] = None

        # Create root node (initial state)
        self._create_root()

    def _create_root(self) -> None:
        """Create the root node (initial state, no action)."""
        root = UndoNode(
            description="Initial state",
            branch_name="main",
        )
        self._nodes[root.id] = root
        self._root_node_id = root.id
        self._current_node_id = root.id

    def push(self, action: UndoAction, branch_name: str = "") -> str:
        """Push a new action, creating a new node.

        Returns:
            The new node ID
        """
        # Determine branch name
        current = self.get_current_node()
        if branch_name:
            new_branch = branch_name
        elif current and current.branch_name:
            new_branch = current.branch_name
        else:
            new_branch = "main"

        # Create new node
        node = UndoNode(
            action=action,
            parent_id=self._current_node_id,
            description=action.description,
            branch_name=new_branch,
        )

        # Add to parent's children
        if self._current_node_id in self._nodes:
            self._nodes[self._current_node_id].children.append(node.id)

        # Store and set as current
        self._nodes[node.id] = node
        self._current_node_id = node.id

        return node.id

    def undo(self) -> Optional[UndoAction]:
        """Undo: move to parent node and execute undo.

        Returns:
            The undone action, or None if at root
        """
        current = self.get_current_node()
        if not current or current.is_root():
            return None

        # Execute undo
        if current.action:
            # AUD-5412: a failing callback must not leave the cursor on the
            # node — retrying would re-run the (possibly partially applied)
            # action again.  Log the failure and still advance the cursor so
            # it stays consistent with the applied prefix.
            try:
                current.action.undo()
            except Exception as e:
                logger.error(
                    "Undo callback failed for action %r (%s): %s",
                    current.action.description, current.action.action_type, e,
                    exc_info=True)

        # Move to parent
        self._current_node_id = current.parent_id

        return current.action

    def redo(self) -> Optional[UndoAction]:
        """Redo: move to next node and execute redo.

        If there are multiple children (branches), chooses based on:
        1. Selected branch (if set via select_branch())
        2. Most recent child (default fallback)

        Returns:
            The redone action, or None if no children
        """
        current = self.get_current_node()
        if not current or not current.children:
            return None

        # Determine which child to choose
        child_id = None

        # Priority 1: Use selected branch if set and available
        if self._selected_branch:
            for cid in current.children:
                child = self._nodes.get(cid)
                if child and child.branch_name == self._selected_branch:
                    child_id = cid
                    break

        # Priority 2: Fallback to most recent child
        if child_id is None:
            child_id = current.children[-1]

        child = self._nodes.get(child_id)

        if child and child.action:
            # AUD-5412: same protection as undo() — advance the cursor even
            # when the redo callback fails, so a retry never re-applies a
            # partially applied action.
            try:
                child.action.redo()
            except Exception as e:
                logger.error(
                    "Redo callback failed for action %r (%s): %s",
                    child.action.description, child.action.action_type, e,
                    exc_info=True)
            self._current_node_id = child_id
            # Clear selected branch after using it (branch selection is one-shot)
            self._selected_branch = None
            return child.action

        return None

    def redo_to_branch(self, branch_name: str) -> Optional[UndoAction]:
        """Redo to a specific branch.

        Args:
            branch_name: Branch to switch to

        Returns:
            The redone action, or None if branch not found
        """
        current = self.get_current_node()
        if not current or not current.children:
            return None

        # Find child with matching branch name
        for child_id in current.children:
            child = self._nodes.get(child_id)
            if child and child.branch_name == branch_name:
                if child.action:
                    # AUD-5412: same callback protection as redo().
                    try:
                        child.action.redo()
                    except Exception as e:
                        logger.error(
                            "Redo-to-branch callback failed for action %r "
                            "(%s): %s", child.action.description,
                            child.action.action_type, e, exc_info=True)
                    self._current_node_id = child_id
                    return child.action

        return None

    def select_branch(self, branch_name: str) -> bool:
        """Select a branch for the next redo operation.

        This method allows users to choose which branch to follow when
        multiple branches exist at the current node. The selection is
        one-shot: it affects only the next redo() call.

        Unlike redo_to_branch(), this method does not execute the redo
        immediately. It only marks the preferred branch for the next
        redo() call.

        Args:
            branch_name: Branch to select for next redo.

        Returns:
            True if the branch exists and can be selected, False otherwise.

        Example:
            # User wants to switch to branch "experimental"
            tree.select_branch("experimental")
            # Next redo() will follow "experimental" branch
            tree.redo()
        """
        current = self.get_current_node()
        if not current or not current.children:
            return False

        # Verify branch exists at current position
        for child_id in current.children:
            child = self._nodes.get(child_id)
            if child and child.branch_name == branch_name:
                self._selected_branch = branch_name
                return True

        return False

    def get_selected_branch(self) -> Optional[str]:
        """Get the currently selected branch name.

        Returns:
            Selected branch name, or None if no branch is selected.
        """
        return self._selected_branch

    def clear_branch_selection(self) -> None:
        """Clear any pending branch selection.

        Resets the branch selection so the next redo() will use
        the default behavior (most recent child).
        """
        self._selected_branch = None

    def get_available_branches_at_current(self) -> List[Tuple[str, str, str]]:
        """Get detailed information about available branches at current position.

        Returns a list of tuples with branch information for UI display.

        Returns:
            List of (branch_name, node_id, description) tuples for each
            branch available at the current position.

        Example:
            branches = tree.get_available_branches_at_current()
            # branches might be [("main", "node_123", "Change radius"),
            #                     ("experimental", "node_456", "Change blur")]
        """
        current = self.get_current_node()
        if not current or not current.children:
            return []

        branches = []
        for child_id in current.children:
            child = self._nodes.get(child_id)
            if child:
                branches.append((child.branch_name, child_id, child.description))

        return branches

    def switch_to_node(self, node_id: str) -> bool:
        """Switch to a specific node in history.

        This involves:
        1. Undo all actions from current to common ancestor
        2. Redo all actions from common ancestor to target

        Args:
            node_id: Target node ID

        Returns:
            True if switch successful
        """
        if node_id not in self._nodes:
            return False

        if node_id == self._current_node_id:
            return True  # Already at target

        # Find path from current to target
        current_path = self._get_path_to_root(self._current_node_id)
        target_path = self._get_path_to_root(node_id)

        # Find common ancestor
        common_ancestor_id = None
        for nid in current_path:
            if nid in target_path:
                common_ancestor_id = nid
                break

        if common_ancestor_id is None:
            return False  # No common ancestor (shouldn't happen)

        # Undo from current to common ancestor
        while self._current_node_id != common_ancestor_id:
            self.undo()

        # Redo from common ancestor to target
        # Find the sub-path from ancestor to target
        target_subpath = []
        for nid in target_path:
            if nid == common_ancestor_id:
                break
            target_subpath.append(nid)

        # Reverse to get forward order
        target_subpath.reverse()

        for nid in target_subpath:
            node = self._nodes.get(nid)
            if node and node.action:
                # AUD-5412: same callback protection as undo()/redo().
                try:
                    node.action.redo()
                except Exception as e:
                    logger.error(
                        "Switch-to-node redo callback failed for action %r "
                        "(%s): %s", node.action.description,
                        node.action.action_type, e, exc_info=True)
                self._current_node_id = nid

        return True

    def _remove_node_preserving_chain(self, node_id: str) -> bool:
        """Remove a node, re-attaching its children to its parent.

        AUD-5409/AUD-5410: any history restructuring must keep parent/child
        chains complete. Removing a node without re-linking its descendants
        leaves dangling parent_ids — undo-to-bottom parks the cursor in a
        ghost node, redo breaks permanently, and keep_types nodes become
        unreachable orphans (the AUD-5408 cascade).

        Args:
            node_id: The node to remove. The root node is never removed.

        Returns:
            True if the node was removed, False if it was not found or is
            the root.
        """
        node = self._nodes.get(node_id)
        if node is None or node_id == self._root_node_id:
            return False

        parent: Optional[UndoNode] = None
        if node.parent_id:
            parent = self._nodes.get(node.parent_id)
        if parent is None:
            # Missing parent (corrupted tree): adopt the root so the
            # surviving descendants stay connected to the history.
            parent = self._nodes.get(self._root_node_id)

        promoted: List[str] = list(node.children)
        if parent is not None:
            if node_id in parent.children:
                # Splice promoted children in where the removed node sat to
                # preserve branch ordering.
                idx = parent.children.index(node_id)
                parent.children[idx:idx + 1] = promoted
            else:
                parent.children.extend(promoted)

        for child_id in promoted:
            child = self._nodes.get(child_id)
            if child is not None:
                child.parent_id = parent.id if parent is not None else None

        if self._current_node_id == node_id:
            self._current_node_id = (
                parent.id if parent is not None else self._root_node_id)

        del self._nodes[node_id]
        return True

    def _get_path_to_root(self, node_id: str) -> List[str]:
        """Get path from node to root (inclusive)."""
        path = []
        current_id = node_id
        while current_id:
            path.append(current_id)
            node = self._nodes.get(current_id)
            if node:
                current_id = node.parent_id
            else:
                break
        # AUD-5409: detect a broken parent chain (hole). Truncation here
        # means some ancestor id is gone from the tree — undo to bottom
        # would fall off the path and redo would break.
        if path and path[-1] != self._root_node_id:
            logger.warning(
                "UndoTree: path from %r stops at dangling node %r "
                "(root is %r) — history chain has a hole",
                node_id, path[-1], self._root_node_id)
        return path

    def get_current_node(self) -> Optional[UndoNode]:
        """Get the current node."""
        return self._nodes.get(self._current_node_id)

    def get_root_node(self) -> UndoNode:
        """Get the root node."""
        return self._nodes[self._root_node_id]

    def get_branches_at_current(self) -> List[str]:
        """Get all branch names available at current position."""
        current = self.get_current_node()
        if not current or not current.children:
            return []

        branches = []
        for child_id in current.children:
            child = self._nodes.get(child_id)
            if child and child.branch_name not in branches:
                branches.append(child.branch_name)

        return branches

    def get_history_list(self) -> List[Tuple[str, str, datetime]]:
        """Get history as a list of (node_id, description, timestamp).

        Returns history from root to current position.
        """
        path = self._get_path_to_root(self._current_node_id)
        path.reverse()  # From root to current

        history = []
        for nid in path:
            node = self._nodes.get(nid)
            if node:
                history.append((nid, node.description, node.timestamp))

        return history

    def get_all_branches(self) -> Dict[str, List[str]]:
        """Get all branches and their node IDs.

        Returns:
            Dict mapping branch_name to list of node IDs
        """
        branches: Dict[str, List[str]] = {}
        for node in self._nodes.values():
            if node.branch_name not in branches:
                branches[node.branch_name] = []
            branches[node.branch_name].append(node.id)

        return branches

    def clear(self) -> None:
        """Clear all history except root."""
        self._nodes.clear()
        self._create_root()

    def to_dict(self) -> dict:
        """Serialize tree for persistence."""
        return {
            "nodes": {nid: node.to_dict() for nid, node in self._nodes.items()},
            "root_node_id": self._root_node_id,
            "current_node_id": self._current_node_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'UndoTree':
        """Deserialize tree from dict."""
        tree = cls.__new__(cls)
        tree._nodes = {}
        tree._root_node_id = data.get("root_node_id", "")
        tree._current_node_id = data.get("current_node_id", "")
        tree._selected_branch = None  # Branch selection is not persisted

        for nid, node_data in data.get("nodes", {}).items():
            tree._nodes[nid] = UndoNode.from_dict(node_data)

        return tree

    def get_nodes_count(self) -> int:
        """Get total number of nodes (excluding root)."""
        return len(self._nodes) - 1  # Exclude root

    def compress(self, keep_types: List[str] = None, max_nodes: int = 50) -> None:
        """Compress history by merging consecutive mergeable actions.

        Strategy:
        1. Keep actions of specified types (e.g., add_node, remove_node)
        2. Merge consecutive param_changes with same merge_key
        3. Limit total nodes to max_nodes

        Args:
            keep_types: Action types to always keep (critical actions)
            max_nodes: Maximum nodes after compression
        """
        if keep_types is None:
            keep_types = ["add_node", "remove_node", "connect", "disconnect"]

        # Walk through history and merge where possible
        path = self._get_path_to_root(self._current_node_id)
        path.reverse()  # From root to current

        merged_count = 0

        # AUD-5408: iterate by index instead of range() so a merged-away
        # node can be spliced out of the walk while the surviving node keeps
        # merging with the next successor on the path.
        i = 0
        while i < len(path) - 1:
            node_id = path[i]
            next_node_id = path[i + 1]

            node = self._nodes.get(node_id)
            next_node = self._nodes.get(next_node_id)

            if not node or not next_node:
                i += 1
                continue

            # Skip if action type is in keep_types
            if next_node.action and next_node.action.action_type in keep_types:
                i += 1
                continue

            # Try to merge consecutive param_changes
            if (node.action and next_node.action and
                node.action.can_merge_with(next_node.action)):
                # Merge next_node into node
                merged = node.action.merge_with(next_node.action)
                node.action = merged
                merged_count += 1

                # AUD-5408: re-attach next_node's children to node before
                # deleting it.  Without this, every descendant (including
                # the undo cursor when it sits deeper in the path) is left
                # with a dangling parent_id: _get_path_to_root stops at the
                # hole, redo breaks permanently and the merged action
                # becomes unreachable.
                promoted: List[str] = list(next_node.children)
                for child_id in promoted:
                    child = self._nodes.get(child_id)
                    if child is not None:
                        child.parent_id = node_id
                if next_node_id in node.children:
                    # Splice the promoted children into node.children right
                    # where next_node_id sat to preserve branch ordering.
                    idx = node.children.index(next_node_id)
                    node.children[idx:idx + 1] = promoted
                else:
                    node.children.extend(promoted)
                    if next_node.parent_id:
                        parent = self._nodes.get(next_node.parent_id)
                        if parent and next_node_id in parent.children:
                            parent.children.remove(next_node_id)

                # Remove next_node from tree
                del self._nodes[next_node_id]

                # Update current_node_id if needed
                if self._current_node_id == next_node_id:
                    self._current_node_id = node_id

                # Keep walking from the surviving node: its merged action
                # may absorb the next successor as well.
                del path[i + 1]
                continue

            i += 1

        # If still over limit, prune oldest non-critical nodes.
        # 两级淘汰 + 兜底退出：
        #   1) 先淘汰非 keep_types 的最旧节点；
        #   2) 若候选耗尽仍超限（例如历史全是 connect/disconnect 这类
        #      keep_types 操作），退而淘汰最旧的非 root 节点——否则
        #      ``while`` 找不到候选将永远空转（UI 线程 100% CPU 冻结，
        #      快速连续连线 >MAX_HISTORY 次即可触发）。
        #   3) 只剩 root/当前节点时无条件退出（内存上限退化为软限制，
        #      好过死循环）。
        while self.get_nodes_count() > max_nodes:
            oldest_id = None
            oldest_time = None

            for nid, node in self._nodes.items():
                if node.is_root():
                    continue
                if nid == self._current_node_id:
                    continue  # 永不淘汰当前节点（undo/redo 游标）
                if node.action and node.action.action_type in keep_types:
                    continue

                if oldest_time is None or node.timestamp < oldest_time:
                    oldest_time = node.timestamp
                    oldest_id = nid

            if oldest_id is None:
                # 退化：只剩 keep_types / root / 当前节点。为守住硬上限，
                # 无视 keep_types 再找一轮；仍无候选则放弃压缩。
                for nid, node in self._nodes.items():
                    if node.is_root() or nid == self._current_node_id:
                        continue
                    if oldest_time is None or node.timestamp < oldest_time:
                        oldest_time = node.timestamp
                        oldest_id = nid
            if oldest_id is None:
                break

            # AUD-5409: evict via the chain-preserving removal. The old code
            # only unlinked the node from its parent's children and deleted
            # it — the evicted node's own children kept a dangling parent_id,
            # so evicting an ancestor of the cursor (possible: only root and
            # the current node were protected) left the undo path holed and
            # turned surviving keep_types nodes into unreachable orphans.
            if not self._remove_node_preserving_chain(oldest_id):
                break


class UndoStack(QObject):
    """High-level undo/redo API wrapping UndoTree.

    Features:
    - Command merging (automatic coalescing)
    - Command compression (automatic when exceeding limit)
    - Branch undo (non-linear history)
    - Persistence (save/load to disk)
    - Grouping (batch actions as single undo)
    """

    changed = pyqtSignal()  # Emitted when the stack changes
    graph_changed = pyqtSignal()  # Emitted when a graph-affecting action is undone/redone

    MAX_HISTORY = 50
    MERGE_WINDOW_MS = 500  # Default merge window

    # Action types that are always kept during compression
    KEEP_TYPES = ["add_node", "remove_node", "connect", "disconnect", "group"]

    def __init__(self, parent=None, persistence_path: str = None) -> None:
        super().__init__(parent)
        self._tree = UndoTree()
        self._persistence_path = persistence_path

        # Grouping support
        self._group_active = False
        self._group_actions: List[UndoAction] = []

        # Merge support: track pending merge action
        self._pending_merge_action: Optional[UndoAction] = None
        self._merge_coalescing = False  # Flag for merge coalescing mode

        # Compression threshold
        self._compression_threshold = self.MAX_HISTORY

        # Callback registry for persistence (action_type -> (undo_fn, redo_fn))
        self._callback_registry: Dict[str, Tuple[Callable, Callable]] = {}

    def register_callbacks(self, action_type: str, undo_fn: Callable, redo_fn: Callable) -> None:
        """Register callbacks for an action type (for persistence restoration)."""
        self._callback_registry[action_type] = (undo_fn, redo_fn)

    def push(self, action: UndoAction) -> None:
        """Push an action onto the undo stack.

        Handles:
        - Merging: If in merge coalescing mode, accumulate instead of push
        - Grouping: If group active, accumulate instead of push
        - Compression: Auto-compress if exceeding threshold
        """
        # Handle grouping
        if self._group_active:
            self._group_actions.append(action)
            return

        # Handle merging: accumulate instead of push
        if action.is_mergeable() and self._merge_coalescing:
            if self._pending_merge_action is None:
                # First action in merge sequence - store it
                self._pending_merge_action = action
            else:
                # Merge with previous pending action
                if self._pending_merge_action.can_merge_with(action):
                    self._pending_merge_action = self._pending_merge_action.merge_with(action)
                else:
                    # Cannot merge - push pending and start new
                    self._tree.push(self._pending_merge_action)
                    self._pending_merge_action = action
            self.changed.emit()
            return

        # Normal push
        self._tree.push(action)

        # Auto-compress if needed
        if self._tree.get_nodes_count() > self._compression_threshold:
            self._tree.compress(keep_types=self.KEEP_TYPES, max_nodes=self.MAX_HISTORY)

        self.changed.emit()

    def begin_merge_coalescing(self) -> None:
        """Begin merge coalescing mode.

        In this mode, consecutive mergeable actions are merged instead of pushed.
        Useful for slider dragging (continuous param changes).
        """
        self._merge_coalescing = True
        self._pending_merge_action = None  # Reset pending action

    def end_merge_coalescing(self) -> None:
        """End merge coalescing mode.

        Pushes any pending merged action onto the tree.
        """
        self._merge_coalescing = False

        # Push any pending merged action
        if self._pending_merge_action is not None:
            self._tree.push(self._pending_merge_action)
            self._pending_merge_action = None

            # Auto-compress if needed
            if self._tree.get_nodes_count() > self._compression_threshold:
                self._tree.compress(keep_types=self.KEEP_TYPES, max_nodes=self.MAX_HISTORY)

            self.changed.emit()

    def begin_group(self) -> None:
        """Start grouping actions."""
        self._group_active = True
        self._group_actions = []

    def end_group(self, description: str = "") -> None:
        """End grouping and push all grouped actions as a single undo."""
        self._group_active = False
        if not self._group_actions:
            return

        actions = list(self._group_actions)
        self._group_actions = []

        desc = description or f"{len(actions)} actions"

        # Create grouped action
        group_undo = UndoAction(
            "group",
            {"actions": actions},
            lambda d: self._undo_group(d["actions"]),
            lambda d: self._redo_group(d["actions"]),
            desc,
        )
        self.push(group_undo)

    def _undo_group(self, actions: list) -> None:
        """Undo a group of actions in reverse order.

        AUD-5412: a failing sub-action no longer aborts the loop. The
        failure is logged (with the applied-prefix position) and the
        remaining older sub-actions still run, so the group gets as close
        to its pre-group state as possible and a retry never replays the
        already-applied prefix.
        """
        total = len(actions)
        applied = 0
        for index in range(total - 1, -1, -1):
            action = actions[index]
            try:
                action.undo()
            except Exception as e:
                logger.error(
                    "Group undo: sub-action %d/%d (%r, %s) failed after "
                    "%d applied: %s", index, total, action.description,
                    action.action_type, applied, e, exc_info=True)
            applied += 1

    def _redo_group(self, actions: list) -> None:
        """Redo a group of actions in forward order.

        AUD-5412: same prefix protection as _undo_group().
        """
        total = len(actions)
        for index, action in enumerate(actions):
            try:
                action.redo()
            except Exception as e:
                logger.error(
                    "Group redo: sub-action %d/%d (%r, %s) failed after "
                    "%d applied: %s", index, total, action.description,
                    action.action_type, index, e, exc_info=True)

    def undo(self) -> Optional[UndoAction]:
        """Undo the last action."""
        action = self._tree.undo()
        if action:
            self.changed.emit()
            self.graph_changed.emit()
        return action

    def redo(self) -> Optional[UndoAction]:
        """Redo the next action."""
        action = self._tree.redo()
        if action:
            self.changed.emit()
            self.graph_changed.emit()
        return action

    def redo_to_branch(self, branch_name: str) -> Optional[UndoAction]:
        """Redo to a specific branch."""
        action = self._tree.redo_to_branch(branch_name)
        if action:
            self.changed.emit()
            self.graph_changed.emit()
        return action

    def switch_to_node(self, node_id: str) -> bool:
        """Switch to a specific node in history."""
        success = self._tree.switch_to_node(node_id)
        if success:
            self.changed.emit()
            self.graph_changed.emit()
        return success

    def can_undo(self) -> bool:
        """Check if undo is possible."""
        current = self._tree.get_current_node()
        return current is not None and not current.is_root()

    def can_redo(self) -> bool:
        """Check if redo is possible."""
        current = self._tree.get_current_node()
        return current is not None and len(current.children) > 0

    def clear(self) -> None:
        """Clear all undo/redo history."""
        self._tree.clear()
        self.changed.emit()

    def undo_description(self) -> str:
        """Get description of action to be undone."""
        current = self._tree.get_current_node()
        if current and current.action:
            return f"Undo: {current.action.description}"
        return ""

    def redo_description(self) -> str:
        """Get description of action to be redone."""
        current = self._tree.get_current_node()
        if current and current.children:
            # Get first child's description
            child_id = current.children[-1]
            child = self._tree._nodes.get(child_id)
            if child and child.action:
                return f"Redo: {child.action.description}"
        return ""

    def get_history_list(self) -> List[Tuple[str, str, datetime]]:
        """Get history as list of (node_id, description, timestamp)."""
        return self._tree.get_history_list()

    def get_branches_at_current(self) -> List[str]:
        """Get available branches at current position."""
        return self._tree.get_branches_at_current()

    def get_all_branches(self) -> Dict[str, List[str]]:
        """Get all branches."""
        return self._tree.get_all_branches()

    def select_branch(self, branch_name: str) -> bool:
        """Select a branch for the next redo operation.

        This method allows users to choose which branch to follow when
        multiple branches exist at the current node. The selection is
        one-shot: it affects only the next redo() call.

        Args:
            branch_name: Branch to select for next redo.

        Returns:
            True if the branch exists and can be selected, False otherwise.
        """
        success = self._tree.select_branch(branch_name)
        if success:
            self.changed.emit()
        return success

    def get_selected_branch(self) -> Optional[str]:
        """Get the currently selected branch name.

        Returns:
            Selected branch name, or None if no branch is selected.
        """
        return self._tree.get_selected_branch()

    def clear_branch_selection(self) -> None:
        """Clear any pending branch selection."""
        self._tree.clear_branch_selection()
        self.changed.emit()

    def get_available_branches_at_current(self) -> List[Tuple[str, str, str]]:
        """Get detailed information about available branches at current position.

        Returns a list of tuples with branch information for UI display.

        Returns:
            List of (branch_name, node_id, description) tuples for each
            branch available at the current position.
        """
        return self._tree.get_available_branches_at_current()

    def get_current_node_id(self) -> str:
        """Get current node ID."""
        return self._tree._current_node_id

    def compress(self) -> None:
        """Manually trigger compression."""
        self._tree.compress(keep_types=self.KEEP_TYPES, max_nodes=self.MAX_HISTORY)
        self.changed.emit()

    # ── Persistence ──

    @staticmethod
    def _history_json_default(obj: Any) -> Any:
        """json.dump default hook (AUD-5411).

        Parameter values recorded in undo data can carry numpy scalars /
        0-d arrays (and similar non-JSON numerics). Without this hook the
        serialization of the WHOLE history file fails with TypeError.
        """
        item = getattr(obj, "item", None)
        if callable(item):
            try:
                return item()
            except (TypeError, ValueError):
                pass
        raise TypeError(
            f"Object of type {type(obj).__name__} is not JSON serializable")

    def save_to_file(self, path: str = None) -> bool:
        """Save undo history to file.

        Args:
            path: File path (uses persistence_path if not provided)

        Returns:
            True if save successful
        """
        if path is None:
            path = self._persistence_path

        if not path:
            return False

        try:
            data = self._tree.to_dict()

            # Serialize actions (without callbacks)
            # Note: callbacks need to be restored on load

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=self._history_json_default)

            return True

        except OSError as e:
            # AUD-5411: distinguish I/O failures from serialization
            # failures and log at error level — a silently swallowed save
            # failure loses the whole undo history.
            logger.error("Undo history save failed (I/O, path=%s): %s",
                         path, e)
            return False
        except TypeError as e:
            logger.error("Undo history save failed (serialization): %s", e)
            return False
        except Exception as e:
            logger.error("Undo history save failed: %s", e, exc_info=True)
            return False

    def _restore_action(self, action_data: dict) -> Optional[UndoAction]:
        """Rebuild one serialized UndoAction (AUD-5410).

        Recurses into "group" actions so grouped sub-actions come back as
        real UndoActions (the group callbacks iterate and call
        action.undo()/redo() on each of them).

        Args:
            action_data: The serialized action payload.

        Returns:
            The restored action, or None when the action_type has no
            registered callbacks or the payload is malformed.
        """
        if not isinstance(action_data, dict):
            return None
        action_type = action_data.get("action_type")
        if not isinstance(action_type, str) or not action_type:
            return None

        entry = self._callback_registry.get(action_type)
        if action_type == "group":
            if entry is not None:
                undo_fn, redo_fn = entry
            else:
                # Same wiring end_group() uses; registered as the default so
                # persisted groups restore even without an explicit
                # register_callbacks("group", ...) call.
                def _group_undo(d: dict) -> None:
                    self._undo_group(d["actions"])

                def _group_redo(d: dict) -> None:
                    self._redo_group(d["actions"])

                undo_fn, redo_fn = _group_undo, _group_redo
        elif entry is None:
            return None
        else:
            undo_fn, redo_fn = entry

        data = action_data.get("data")
        if action_type == "group" and isinstance(data, dict):
            actions = data.get("actions")
            if isinstance(actions, list):
                restored_subs: List[UndoAction] = []
                for sub in actions:
                    if isinstance(sub, UndoAction):
                        restored_subs.append(sub)
                        continue
                    restored = self._restore_action(sub)
                    if restored is not None:
                        restored_subs.append(restored)
                    else:
                        logger.warning(
                            "UndoStack: dropping unrestorable group "
                            "sub-action (type %r)",
                            sub.get("action_type") if isinstance(sub, dict)
                            else type(sub).__name__)
                data = dict(data)
                data["actions"] = restored_subs

        try:
            return UndoAction.from_dict(
                {**action_data, "data": data}, undo_fn, redo_fn)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("UndoStack: failed to restore action %r: %s",
                           action_type, e)
            return None

    def load_from_file(self, path: str = None) -> bool:
        """Load undo history from file.

        Args:
            path: File path (uses persistence_path if not provided)

        Returns:
            True if load successful
        """
        if path is None:
            path = self._persistence_path

        if not path or not os.path.exists(path):
            return False

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Restore tree structure
            self._tree = UndoTree.from_dict(data)

            # Restore actions with callbacks.
            # AUD-5410: UndoNode.from_dict now parks the serialized action
            # payload on the node (pending_action), so this loop actually
            # runs. Previously node.action was always None here — the
            # restoration was dead code and a loaded history could never
            # execute anything (undo moved the cursor without applying,
            # can_redo reported True while redo did nothing).
            for node_id, node in list(self._tree._nodes.items()):
                pending = node.pending_action
                if not pending:
                    continue
                node.pending_action = None
                action = self._restore_action(pending)
                if action is not None:
                    node.action = action
                else:
                    # Prune nodes that cannot execute: keeping them as
                    # action=None ghosts freezes can_redo at True while
                    # redo() refuses to advance. Children are re-attached
                    # so the chain stays complete.
                    logger.warning(
                        "UndoStack: pruning history node %r (action_type "
                        "%r has no registered callbacks)",
                        node_id, pending.get("action_type"))
                    self._tree._remove_node_preserving_chain(node_id)

            self.changed.emit()
            return True

        except Exception as e:
            logger.error("Undo history load failed (path=%s): %s",
                         path, e, exc_info=True)
            return False

    def set_persistence_path(self, path: str) -> None:
        """Set default persistence path."""
        self._persistence_path = path

    def auto_save(self) -> bool:
        """Auto-save to persistence path."""
        return self.save_to_file()

    def auto_load(self) -> bool:
        """Auto-load from persistence path."""
        return self.load_from_file()


# ── Factory Functions for Common Actions ──

def create_param_change_action(
    node_id: str,
    param_name: str,
    old_value: Any,
    new_value: Any,
    undo_fn: Callable,
    redo_fn: Callable,
    merge_window_ms: int = 500
) -> UndoAction:
    """Create a parameter change action with merge support.

    This is a helper function to create mergeable param_change actions.
    """
    merge_key = f"param_change:{node_id}:{param_name}"

    return UndoAction(
        "param_change",
        {
            "node_id": node_id,
            "param": param_name,
            "old": old_value,
            "new": new_value,
        },
        undo_fn,
        redo_fn,
        f"Change {param_name}",
        merge_key=merge_key,
        merge_window_ms=merge_window_ms,
    )


def create_move_nodes_action(
    node_ids: List[str],
    old_positions: List[Tuple[float, float]],
    new_positions: List[Tuple[float, float]],
    undo_fn: Callable,
    redo_fn: Callable,
    merge_window_ms: int = 500
) -> UndoAction:
    """Create a move nodes action with merge support."""
    # Merge key combines all node_ids for batch merging
    merge_key = f"move_nodes:{','.join(node_ids)}"

    return UndoAction(
        "move_nodes",
        {
            "node_ids": node_ids,
            "old_positions": old_positions,
            "new_positions": new_positions,
        },
        undo_fn,
        redo_fn,
        "Move nodes",
        merge_key=merge_key,
        merge_window_ms=merge_window_ms,
    )


def create_add_node_action(
    node_data: dict,
    undo_fn: Callable,
    redo_fn: Callable,
) -> UndoAction:
    """Create an add node action (not mergeable, always kept)."""
    return UndoAction(
        "add_node",
        node_data,
        undo_fn,
        redo_fn,
        f"Add {node_data.get('node_type', 'node')}",
        merge_key=None,  # Not mergeable
    )


def create_remove_node_action(
    node_data: dict,
    undo_fn: Callable,
    redo_fn: Callable,
) -> UndoAction:
    """Create a remove node action (not mergeable, always kept)."""
    return UndoAction(
        "remove_node",
        node_data,
        undo_fn,
        redo_fn,
        f"Remove {node_data.get('node_type', 'node')}",
        merge_key=None,  # Not mergeable
    )


def create_connect_action(
    src_node: str,
    src_port: str,
    dst_node: str,
    dst_port: str,
    undo_fn: Callable,
    redo_fn: Callable,
) -> UndoAction:
    """Create a connect action (not mergeable, always kept)."""
    return UndoAction(
        "connect",
        {
            "src_node": src_node,
            "src_port": src_port,
            "dst_node": dst_node,
            "dst_port": dst_port,
        },
        undo_fn,
        redo_fn,
        f"Connect {src_port} → {dst_port}",
        merge_key=None,
    )


def create_disconnect_action(
    src_node: str,
    src_port: str,
    dst_node: str,
    dst_port: str,
    undo_fn: Callable,
    redo_fn: Callable,
) -> UndoAction:
    """Create a disconnect action (not mergeable, always kept)."""
    return UndoAction(
        "disconnect",
        {
            "src_node": src_node,
            "src_port": src_port,
            "dst_node": dst_node,
            "dst_port": dst_port,
        },
        undo_fn,
        redo_fn,
        f"Disconnect {src_port} → {dst_port}",
        merge_key=None,
    )