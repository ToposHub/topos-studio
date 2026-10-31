"""
EditProject Adapter - Registry for EditProject operations.

This module provides a registry-based adapter pattern so that
`src/shared` never needs to import from `src/features` directly.

At application startup, the features/edit module registers its
concrete implementations here. Shared code then calls through
the registry, maintaining clean layer boundaries.

Usage in src/shared (e.g. offline_asset_manager.py):
    from .edit_project_adapter import edit_project_adapter

    if edit_project_adapter.is_edit_project(project):
        ...

Usage at app startup (e.g. in features/edit/module.py):
    from src.shared.project.edit_project_adapter import edit_project_adapter
    # Import concrete EditProject type and operations from features.edit.core.project

    edit_project_adapter.register(
        edit_project_type=EditProject,
        create_project=EditProject.create,
        relink_asset_fn=relink_asset,
        batch_relink_fn=batch_relink_assets,
        update_offline_status_fn=update_offline_status,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class HasAssets(Protocol):
    """Protocol for objects that have an assets dict with path attribute."""

    assets: dict[str, Any]


class _EditProjectAdapter:
    """
    Registry adapter for EditProject operations.

    All methods are no-ops or return safe defaults until register() is called.
    """

    def __init__(self) -> None:
        self._edit_project_type: type | None = None
        self._create_project: Callable[..., Any] | None = None
        self._relink_asset_fn: Callable[..., Any] | None = None
        self._batch_relink_fn: Callable[..., Any] | None = None
        self._update_offline_status_fn: Callable[..., Any] | None = None
        self._show_relink_dialog_fn: Callable[..., Any] | None = None

    def register(
        self,
        edit_project_type: type,
        create_project: Callable[..., Any],
        relink_asset_fn: Callable[..., Any],
        batch_relink_fn: Callable[..., Any],
        update_offline_status_fn: Callable[..., Any],
        show_relink_dialog_fn: Callable[..., Any] | None = None,
    ) -> None:
        """
        Register concrete EditProject implementations.

        Called once at app startup by the features/edit module.
        """
        self._edit_project_type = edit_project_type
        self._create_project = create_project
        self._relink_asset_fn = relink_asset_fn
        self._batch_relink_fn = batch_relink_fn
        self._update_offline_status_fn = update_offline_status_fn
        self._show_relink_dialog_fn = show_relink_dialog_fn
        logger.debug("EditProjectAdapter registered with type %s", edit_project_type)

    @property
    def is_registered(self) -> bool:
        return self._edit_project_type is not None

    @property
    def edit_project_type(self) -> type:
        if self._edit_project_type is None:
            raise RuntimeError(
                "EditProjectAdapter not registered. "
                "Call edit_project_adapter.register() at app startup."
            )
        return self._edit_project_type

    def is_edit_project(self, obj: Any) -> bool:
        """Check if obj is an EditProject instance."""
        if self._edit_project_type is None:
            return False
        return isinstance(obj, self._edit_project_type)

    def create_project(self, name: str = "Untitled Project") -> Any:
        """Create a new EditProject."""
        if self._create_project is None:
            raise RuntimeError("EditProjectAdapter.create_project not registered")
        return self._create_project(name)

    def relink_asset(self, project: Any, asset_id: str, new_path: str) -> Any:
        """Relink a single asset in an EditProject."""
        if self._relink_asset_fn is None:
            raise RuntimeError("EditProjectAdapter.relink_asset not registered")
        return self._relink_asset_fn(project, asset_id, new_path)

    def batch_relink(self, project: Any, relink_map: dict[str, str]) -> Any:
        """Batch relink assets in an EditProject."""
        if self._batch_relink_fn is None:
            raise RuntimeError("EditProjectAdapter.batch_relink not registered")
        return self._batch_relink_fn(project, relink_map)

    def update_offline_status(self, project: Any) -> Any:
        """Update offline status for all assets in an EditProject."""
        if self._update_offline_status_fn is None:
            raise RuntimeError(
                "EditProjectAdapter.update_offline_status not registered"
            )
        return self._update_offline_status_fn(project)

    def show_relink_dialog(
        self, offline_assets: list[Any], parent: Any = None
    ) -> dict[str, str] | None:
        """Show a relink dialog for offline assets. Returns relink_map or None."""
        if self._show_relink_dialog_fn is None:
            raise RuntimeError("EditProjectAdapter.show_relink_dialog not registered")
        return self._show_relink_dialog_fn(offline_assets, parent)


# Singleton instance
edit_project_adapter = _EditProjectAdapter()


__all__ = ["edit_project_adapter"]
