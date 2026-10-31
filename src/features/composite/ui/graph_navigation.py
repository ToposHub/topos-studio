"""Persistent bookmarks and session navigation for the Composite graph.

The graph canvas has two different kinds of navigation state:

* bookmarks are project UI metadata and must survive save/reopen;
* back/forward history belongs to the current editor session and should not
  make the project payload grow on every pan or zoom.

This module deliberately contains no Qt types so the persistence and history
rules can be tested without constructing a QApplication.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional


def _normalise_viewport(viewport: dict[str, Any]) -> dict[str, float]:
    """Return the stable, numeric portion of a viewport snapshot."""
    if not isinstance(viewport, dict):
        return {"zoom": 1.0, "center_x": 0.0, "center_y": 0.0}

    def number(name: str, default: float) -> float:
        try:
            value = float(viewport.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if value == value else default

    return {
        "zoom": max(0.05, number("zoom", 1.0)),
        "center_x": number("center_x", 0.0),
        "center_y": number("center_y", 0.0),
    }


@dataclass(frozen=True)
class GraphBookmark:
    """A named graph viewport location."""

    name: str
    viewport: dict[str, float]
    selected_node_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the bookmark into project-safe JSON-compatible data."""
        data: dict[str, Any] = {
            "name": self.name,
            "viewport": dict(self.viewport),
        }
        if self.selected_node_id:
            data["selected_node_id"] = self.selected_node_id
        return data

    @classmethod
    def from_dict(cls, data: object) -> Optional["GraphBookmark"]:
        """Parse one bookmark, ignoring malformed external project data."""
        if not isinstance(data, dict):
            return None
        name = str(data.get("name", "")).strip()
        if not name:
            return None
        selected = data.get("selected_node_id")
        selected_node_id = str(selected) if selected else None
        return cls(
            name=name,
            viewport=_normalise_viewport(data.get("viewport", data)),
            selected_node_id=selected_node_id,
        )


class GraphNavigationState:
    """Manage project bookmarks and bounded in-session view history."""

    SCHEMA_VERSION = 1
    DEFAULT_HISTORY_LIMIT = 64

    def __init__(self, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self._history_limit = max(2, int(history_limit))
        self._bookmarks: "OrderedDict[str, GraphBookmark]" = OrderedDict()
        self._history: list[dict[str, Any]] = []
        self._history_index = -1

    # ── Bookmarks ─────────────────────────────────────────────────────

    def add_bookmark(
        self,
        name: str,
        viewport: dict[str, Any],
        selected_node_id: Optional[str] = None,
    ) -> GraphBookmark:
        """Create or replace a bookmark while preserving its menu order."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Bookmark name cannot be empty")
        bookmark = GraphBookmark(
            name=clean_name,
            viewport=_normalise_viewport(viewport),
            selected_node_id=(str(selected_node_id)
                              if selected_node_id else None),
        )
        self._bookmarks[clean_name] = bookmark
        return bookmark

    def remove_bookmark(self, name: str) -> bool:
        """Remove a bookmark and report whether it existed."""
        return self._bookmarks.pop(str(name), None) is not None

    def get_bookmark(self, name: str) -> Optional[GraphBookmark]:
        """Return a bookmark by name."""
        return self._bookmarks.get(str(name))

    def bookmarks(self) -> tuple[GraphBookmark, ...]:
        """Return bookmarks in stable creation/menu order."""
        return tuple(self._bookmarks.values())

    # ── Session history ───────────────────────────────────────────────

    @staticmethod
    def _history_entry(viewport: dict[str, Any],
                       selected_node_id: Optional[str]) -> dict[str, Any]:
        return {
            "viewport": _normalise_viewport(viewport),
            "selected_node_id": (str(selected_node_id)
                                  if selected_node_id else None),
        }

    @staticmethod
    def _same_entry(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return (left.get("viewport") == right.get("viewport")
                and left.get("selected_node_id")
                == right.get("selected_node_id"))

    def record_view(self, viewport: dict[str, Any],
                    selected_node_id: Optional[str] = None) -> bool:
        """Append a changed view, returning False for duplicate snapshots."""
        entry = self._history_entry(viewport, selected_node_id)
        if self._history_index >= 0 and self._same_entry(
                self._history[self._history_index], entry):
            return False

        if self._history_index + 1 < len(self._history):
            self._history = self._history[:self._history_index + 1]
        self._history.append(entry)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]
        self._history_index = len(self._history) - 1
        return True

    def back(self) -> Optional[dict[str, Any]]:
        """Move one step back and return the target snapshot."""
        if self._history_index <= 0:
            return None
        self._history_index -= 1
        return dict(self._history[self._history_index])

    def forward(self) -> Optional[dict[str, Any]]:
        """Move one step forward and return the target snapshot."""
        if self._history_index < 0 or self._history_index + 1 >= len(self._history):
            return None
        self._history_index += 1
        return dict(self._history[self._history_index])

    @property
    def can_go_back(self) -> bool:
        """Whether a previous view is available."""
        return self._history_index > 0

    @property
    def can_go_forward(self) -> bool:
        """Whether a later view is available."""
        return (self._history_index >= 0
                and self._history_index + 1 < len(self._history))

    def clear_history(self) -> None:
        """Drop session history without changing project bookmarks."""
        self._history.clear()
        self._history_index = -1

    # ── Persistence ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize only persistent bookmark data."""
        return {
            "version": self.SCHEMA_VERSION,
            "bookmarks": [bookmark.to_dict() for bookmark in self.bookmarks()],
        }

    @classmethod
    def from_dict(cls, data: object,
                  history_limit: int = DEFAULT_HISTORY_LIMIT
                  ) -> "GraphNavigationState":
        """Load bookmarks defensively from a saved viewport payload."""
        state = cls(history_limit=history_limit)
        if not isinstance(data, dict):
            return state
        raw_bookmarks = data.get("bookmarks", [])
        if isinstance(raw_bookmarks, dict) and "bookmarks" in raw_bookmarks:
            raw_bookmarks = raw_bookmarks.get("bookmarks", [])
        if isinstance(raw_bookmarks, dict):
            raw_bookmarks = list(raw_bookmarks.values())
        if not isinstance(raw_bookmarks, (list, tuple)):
            return state
        for raw in raw_bookmarks:
            bookmark = GraphBookmark.from_dict(raw)
            if bookmark is not None:
                state._bookmarks[bookmark.name] = bookmark
        return state


__all__ = ["GraphBookmark", "GraphNavigationState"]
