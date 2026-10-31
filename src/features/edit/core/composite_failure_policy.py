"""Unified failure policy for Comp Clip rendering errors."""
from __future__ import annotations

from enum import Enum


class CompositeFailureAction(Enum):
    """Action to take when composite rendering fails."""
    SHOW_ERROR_OVERLAY = "show_error_overlay"
    SHOW_SOURCE_GHOST = "show_source_ghost"
    RAISE_ERROR = "raise_error"


class CompositeFailurePolicy:
    """Determines how to handle composite rendering failures per context.

    Policy rules:
    - Edit preview: Show error overlay with optional source ghost
    - Export: Raise error, don't silently output source
    - Render cache: Keep old valid cache, mark as failed
    - Composite page preview-only: Show "Timeline source unavailable" message
    - Normal clip (no composite_id): Show normal source
    """

    @staticmethod
    def for_edit_preview(error: str | None = None) -> dict:
        return {
            "action": CompositeFailureAction.SHOW_ERROR_OVERLAY,
            "message": error or "Composite render error",
            "show_ghost": True,
        }

    @staticmethod
    def for_export(error: str | None = None) -> dict:
        return {
            "action": CompositeFailureAction.RAISE_ERROR,
            "message": error or "Composite render failed during export",
        }

    @staticmethod
    def for_render_cache(error: str | None = None) -> dict:
        return {
            "action": CompositeFailureAction.SHOW_ERROR_OVERLAY,
            "message": error or "Render cache failed",
            "keep_old_cache": True,
        }

    @staticmethod
    def for_composite_page_no_source() -> dict:
        return {
            "action": CompositeFailureAction.SHOW_ERROR_OVERLAY,
            "message": "Timeline source unavailable",
        }
