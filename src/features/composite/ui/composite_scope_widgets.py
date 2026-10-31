"""Composite entry point for the shared professional Scopes panel.

The Color and Composite pages intentionally use the same ``ScopePanel``
implementation. Keeping this import module preserves the Composite feature's
public import path while preventing the two pages from drifting apart in mode
lists, controls, styling, or translations.
"""

from __future__ import annotations

from typing import TypeVar

from src.features.color.ui.scope_panel import MODE_ORDER, SCOPE_MODES, ScopePanel


# The old Composite panel exposed this name to the runtime. It is an alias,
# rather than a subclass, so Qt sees exactly the same widget class and style
# rules on both pages.
CompositeScopePanel = ScopePanel
SCOPE_MODE_ORDER = tuple(MODE_ORDER)


_ScopePanelT = TypeVar("_ScopePanelT", bound=ScopePanel)


def configure_composite_scope_panel(panel: _ScopePanelT) -> _ScopePanelT:
    """Configure the shared ScopePanel for the Composite QC dock.

    Composite scopes run the same native GPU pipeline as Color: the
    Composite renderer owns a shared-context ``GpuScopeEngine`` and answers
    ``ScopeDemand`` submissions with ``ScopeGpuFrame`` texture descriptors
    that the panel's presenters blit directly. Both pages therefore stay on
    the GPU page by default; the CPU page remains the diagnostic fallback,
    exactly like Color.

    Unlike Color, Composite has exactly one scope input — its own rendered
    output. The graded/source/reference selector is therefore limited to
    ``graded`` so the user can never switch to a source that has no data
    path on this page.

    A moderate CPU analysis bound is kept so the fallback page never
    processes a full-resolution readback if it is ever enabled explicitly.
    """
    panel.set_cpu_analysis_bounds(768, 512)
    panel.set_available_sources(('graded',))
    return panel


__all__ = [
    "CompositeScopePanel",
    "MODE_ORDER",
    "SCOPE_MODE_ORDER",
    "SCOPE_MODES",
    "ScopePanel",
    "configure_composite_scope_panel",
]
