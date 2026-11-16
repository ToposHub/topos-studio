"""Immutable presentation contract for Files extensions, independent of a host."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FilesPluginInfo:
    plugin_id: str
    name: str
    version: str
    author: str
    description: str
    api_version: int | None
    category: str
    capabilities: tuple[str, ...]
    runtime_loaded: bool
    desired_enabled: bool
    self_disabled: bool
    needs_restart: bool
    error: str
    launch_path: str
    launch_payload_kind: str
    launch_unavailable_reason: str
    path: Path
    diagnostics: tuple[str, ...] = ()

    @property
    def can_launch(self) -> bool:
        return bool(self.launch_path and not self.launch_unavailable_reason)
