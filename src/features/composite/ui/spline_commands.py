# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Spline edit commands (Undo/Redo) — phase 3 (plan §11.11).

Each command captures the before/after state of a SplineKey (frame, value,
handles, tangent_mode) so undo/redo restores the curve exactly. These are
plain dataclass records consumed by a simple undo stack; the actual QUndoStack
integration lives in CompositePage (mirrors the Keyframes panel approach).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List

if TYPE_CHECKING:  # AUD-10900: 仅为注解引入，运行时不导入（避免潜在环）
    from .spline_model import SplineKey


@dataclass
class KeySnapshot:
    """Immutable snapshot of one SplineKey for undo/redo."""
    frame: int
    value: float
    interpolation: str
    tangent_mode: str
    handle_left: List[float]
    handle_right: List[float]

    @classmethod
    def from_key(cls, key: "SplineKey") -> "KeySnapshot":
        return cls(
            frame=key.frame,
            value=key.value,
            interpolation=key.interpolation,
            tangent_mode=key.tangent_mode,
            handle_left=[key.handle_left.x(), key.handle_left.y()],
            handle_right=[key.handle_right.x(), key.handle_right.y()],
        )

    def apply_to(self, key: "SplineKey") -> None:
        from PyQt6.QtCore import QPointF
        key.frame = self.frame
        key.value = self.value
        key.interpolation = self.interpolation
        key.tangent_mode = self.tangent_mode
        key.handle_left = QPointF(self.handle_left[0], self.handle_left[1])
        key.handle_right = QPointF(self.handle_right[0], self.handle_right[1])


@dataclass
class MoveKeyCommand:
    """Undo record for dragging a keyframe point (frame and/or value)."""
    node_id: str
    param_name: str
    key_index: int
    before: KeySnapshot
    after: KeySnapshot


@dataclass
class MoveHandleCommand:
    """Undo record for dragging a bezier handle."""
    node_id: str
    param_name: str
    key_index: int
    before: KeySnapshot
    after: KeySnapshot
    handle_side: str  # 'left' | 'right'


@dataclass
class SetTangentModeCommand:
    """Undo record for switching a key's tangent mode (smooth/linear/broken...)."""
    node_id: str
    param_name: str
    key_index: int
    before: KeySnapshot
    after: KeySnapshot


@dataclass
class BulkKeyCommand:
    """A composite of several key commands, applied/undone atomically.

    Used for box-select drags that move multiple points at once.
    """
    commands: List[Any] = field(default_factory=list)
