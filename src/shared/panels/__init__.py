"""Panel descriptor and registry for Window > Panels grouping."""

from src.shared.panels.registry import (
    PanelDescriptor,
    PanelRegistry,
    get_panel_registry,
    validate_dock_integrity,
)

__all__ = ["PanelDescriptor", "PanelRegistry", "get_panel_registry", "validate_dock_integrity"]
