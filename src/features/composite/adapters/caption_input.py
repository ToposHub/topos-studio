"""Controlled CaptionCue input adapter for Composite graphs."""

from __future__ import annotations

from typing import Any

from src.features.edit.core.captions.graphics import CaptionCompositeInput, build_composite_input


class CaptionCompositeInputAdapter:
    """Expose a read-only cue snapshot to a Composite instance.

    The adapter deliberately returns a typed value and never accepts text/time
    writes from the graph. Source edits must go through Edit commands.
    """

    @staticmethod
    def resolve(timeline: Any, caption_cue_id: str) -> CaptionCompositeInput:
        return build_composite_input(timeline, caption_cue_id)

    @staticmethod
    def as_node_inputs(timeline: Any, caption_cue_id: str) -> dict[str, Any]:
        value = build_composite_input(timeline, caption_cue_id)
        return {
            "caption_cue_id": value.caption_cue_id,
            "text": value.text,
            "start": value.start,
            "end": value.end,
            "word_timings": [dict(item) for item in value.word_timings],
            "language": value.language,
            "style": dict(value.style),
        }


__all__ = ["CaptionCompositeInputAdapter"]
