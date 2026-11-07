"""Display names never fall back to a node's internal UUID."""
from __future__ import annotations

from typing import Any
from uuid import UUID


def display_node_label(label: Any, *internal_ids: str) -> str:
    """Repair the old UUID-as-default-name artifact; retain custom labels.

    Only an exact match to an owned UUID is considered generated. A custom
    UUID-looking label, or a short test/resource identity, is left intact.
    """
    value = str(label or "")
    if value and value in internal_ids:
        try:
            UUID(value)
        except (ValueError, AttributeError):
            pass
        else:
            return ""
    return value
