# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Export Source Protocol

Defines the interface that export sources must implement.
Allows deliver and other features to work with export sources
without hard-dependending on the edit feature.
"""

from __future__ import annotations

from typing import Protocol


class ExportSource(Protocol):
    """
    Protocol for export sources.

    Any feature that provides frames for export can implement
    this protocol to integrate with the deliver page.
    """

    source_id: str
    title: str

    def is_available(self) -> bool:
        """Return True if this export source is currently available."""
        ...

    def estimate_frames(self) -> int:
        """Return the estimated number of frames available from this source."""
        ...
