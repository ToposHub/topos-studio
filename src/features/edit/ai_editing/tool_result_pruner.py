"""Deterministic, model-facing tool-result pruning.

Tool output is often the largest part of an editing assistant's next context
window: project snapshots, transcripts and subtitle reports can be much larger
than the user prompt.  Keep the full result for audit/replay, but give the
model a bounded head/middle/tail view with an explicit marker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


@dataclass(frozen=True)
class ToolResultPruneConfig:
    """Character budgets for one model-facing tool-result representation."""

    threshold_chars: int = 8192
    head_chars: int = 4096
    tail_chars: int = 1024

    def __post_init__(self) -> None:
        if self.threshold_chars <= 0:
            raise ValueError("threshold_chars must be positive")
        if self.head_chars < 0 or self.tail_chars < 0:
            raise ValueError("head_chars and tail_chars must be non-negative")
        if self.head_chars + len(PRUNE_MARKER) + self.tail_chars > self.threshold_chars:
            raise ValueError("head + marker + tail must fit threshold_chars")


@dataclass(frozen=True)
class PrunedToolResultText:
    """Rendered model text plus boundedness metadata."""

    text: str
    chars_before: int
    chars_after: int
    pruned: bool = False


class ToolResultPruner:
    """Render arbitrary JSON-safe tool output with a deterministic hard cap."""

    def __init__(self, config: ToolResultPruneConfig | None = None) -> None:
        self.config = config or ToolResultPruneConfig()

    def render(self, payload: Any) -> PrunedToolResultText:
        """Serialize *payload* and prune only the model-facing text."""

        text = json.dumps(payload, ensure_ascii=False, default=str)
        return self.prune_text(text)

    def prune_text(self, text: str) -> PrunedToolResultText:
        """Keep the beginning/end of *text* without splitting Unicode code points."""

        value = str(text)
        chars_before = len(value)
        if chars_before <= self.config.threshold_chars:
            return PrunedToolResultText(value, chars_before, chars_before)

        head = value[: self.config.head_chars]
        tail = value[-self.config.tail_chars :] if self.config.tail_chars else ""
        bounded = head + PRUNE_MARKER + tail
        return PrunedToolResultText(
            bounded,
            chars_before,
            len(bounded),
            pruned=True,
        )


__all__ = [
    "PRUNE_MARKER",
    "PrunedToolResultText",
    "ToolResultPruneConfig",
    "ToolResultPruner",
]
