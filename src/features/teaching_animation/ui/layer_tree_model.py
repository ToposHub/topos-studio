"""Qt-free view-model for the layer tree (UX-10).

The layer tree shows page containers, their elements (top-most first, matching
canvas paint order), and structural background rows.  The Freeform HTML source
remains an inspector/rendering concern rather than a user-facing layer.  This
module is the pure, deterministic mapping from a ``TeachingSceneDocument`` to a
list of ``LayerRow`` — no Qt import — so it unit-tests directly and the
``LayerTreePanel`` widget stays a thin renderer on top of it.

Lock / hide / solo are read from the scene's :class:`~..core.hierarchy.Hierarchy`
(``scene.metadata["hierarchy"]``); the row's ``visible`` reflects the *local*
hide flag (``element_id not in hierarchy.hidden``), which is what the eye icon
toggles.  Effective render visibility (honouring hidden ancestors / solo) is the
evaluator's job, already wired in batch SG-01.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.shared.i18n.i18n import I18N

from ..core.hierarchy import read_hierarchy
from ..core.model import ScenePage, TeachingSceneDocument

#: The synthetic id retained for selection/inspector routing.  The HTML source
#: is a document-level implementation detail and is intentionally not rendered
#: as a user-facing layer-tree row.
HTML_LAYER_ROW_ID = "__html_layer__"

#: Prefix for the synthetic page-header rows (``__page__:<page_id>``).  They
#: are not selectable/draggable; clicking one jumps the playhead to the page.
PAGE_ROW_PREFIX = "__page__:"
BACKGROUND_ROW_PREFIX = "__background__:"
GLOBAL_BACKGROUND_ROW_ID = "__global_background__"


def page_row_id(page_id: str) -> str:
    return f"{PAGE_ROW_PREFIX}{page_id}"


def page_background_row_id(page_id: str) -> str:
    return f"{BACKGROUND_ROW_PREFIX}{page_id}"


def page_display_name(page: ScenePage, index: int) -> str:
    """Authored page name, or the localized "Page N" fallback."""
    name = (page.name or "").strip()
    if name:
        return name
    return I18N.tr("motion_graphic.page.page_button", "Page {index}", index=index + 1)


@dataclass(frozen=True)
class LayerRow:
    """One row in the layer tree."""

    id: str                       # element id or synthetic page/background id
    label: str
    kind: str                     # "element" | "page" | "background"
    visible: bool                 # local hide flag (False ⇒ in hierarchy.hidden)
    locked: bool
    solo: bool
    depth: int = 0                # indent level (0 for flat; reserved for nesting)
    is_html: bool = False         # retained for inspector selection compatibility
    page_id: str = ""             # owning page ("page" rows: the page itself)
    page_index: int = -1          # position of the page (page rows only)
    element_count: int = 0        # elements on the page (page rows only)
    active: bool = False          # page row: the playhead is inside this page
    expanded: bool = True         # page row: child rows are visible
    background_value: str = ""    # background row: empty means global fallback

    @property
    def readonly(self) -> bool:
        """Page/background rows are structural and never reordered/removed."""
        return self.is_html or self.kind in {"page", "background"}


def _element_label(element: Any) -> str:
    """A short, human-readable label for an element (content text if any)."""
    content = element.content
    if isinstance(content, str) and content.strip():
        return content.strip()[:40]
    if isinstance(content, Mapping):
        for key in ("text", "title", "label", "kind"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:40]
    return f"{element.type} · {element.element_id}"


def build_layer_rows(scene: TeachingSceneDocument,
                     *, active_page_id: str = "",
                     expanded_page_ids: Iterable[str] | None = None) -> list[LayerRow]:
    """Map a scene to ordered layer-tree rows (top-most element first).

    Ordering: highest ``z_index`` first; equal ``z_index`` breaks by document
    tuple order (later = on top), matching how the canvas paints.  Elements
    are always grouped under page-header rows (pages in timeline order,
    z-descending inside each page).  ``expanded_page_ids`` controls which
    page children are visible; omitted means all pages start expanded.
    """
    hierarchy = read_hierarchy(scene.metadata)
    locked = frozenset(hierarchy.locked)
    solo = frozenset(hierarchy.solo)
    hidden = frozenset(hierarchy.hidden)

    # Stable sort: (z_index, original_index) both descending → top of canvas first.
    ordered = sorted(
        enumerate(scene.elements),
        key=lambda pair: (pair[1].z_index, pair[0]),
        reverse=True,
    )
    expanded_pages = (
        frozenset(scene.page_ids)
        if expanded_page_ids is None else frozenset(expanded_page_ids)
    )
    extra_depth = 1

    def _element_row(element: Any) -> LayerRow:
        return LayerRow(
            id=element.element_id,
            label=_element_label(element),
            kind="element",
            visible=element.element_id not in hidden,
            locked=element.element_id in locked,
            solo=element.element_id in solo,
            depth=len(hierarchy.ancestors(element.element_id)) + extra_depth,
            page_id=element.page_id,
        )

    rows: list[LayerRow] = []
    for index, page in enumerate(scene.pages):
        page_elements = [element for _idx, element in ordered
                         if element.page_id == page.page_id]
        is_expanded = page.page_id in expanded_pages
        rows.append(LayerRow(
            id=page_row_id(page.page_id),
            label=page_display_name(page, index),
            kind="page",
            visible=True,
            locked=False,
            solo=False,
            page_id=page.page_id,
            page_index=index,
            element_count=len(page_elements),
            active=page.page_id == active_page_id,
            expanded=is_expanded,
        ))
        if is_expanded:
            rows.extend(_element_row(element) for element in page_elements)
            rows.append(LayerRow(
                id=page_background_row_id(page.page_id),
                label=I18N.tr("motion_graphic.page.background", "Background"),
                kind="background",
                visible=True,
                locked=False,
                solo=False,
                depth=1,
                page_id=page.page_id,
                background_value=page.background,
            ))
    rows.append(LayerRow(
        id=GLOBAL_BACKGROUND_ROW_ID,
        label=I18N.tr("motion_graphic.page.global_background", "Global Background"),
        kind="background",
        visible=True,
        locked=False,
        solo=False,
        background_value=str(
            scene.background.get("css", scene.background.get("color", ""))
            if isinstance(scene.background, Mapping) else ""
        ),
    ))
    return rows


__all__ = [
    "HTML_LAYER_ROW_ID", "PAGE_ROW_PREFIX", "BACKGROUND_ROW_PREFIX",
    "GLOBAL_BACKGROUND_ROW_ID", "LayerRow", "build_layer_rows",
    "page_display_name", "page_row_id", "page_background_row_id",
]
