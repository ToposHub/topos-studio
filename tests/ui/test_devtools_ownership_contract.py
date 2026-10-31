"""R8: DevTools Apply ownership contract (DOM ownership never drifts).

``read_authored_document`` must strip every host-injected scaffolding node so
an Apply can only ever write the template author's own DOM back into the HTML
layer — never the editor's generated overlay, draw surface, page-deck runtime
or scrollbar chrome.  These ids are injected from several modules as string
literals; this contract test fails the build the moment someone adds a new
scaffolding node without teaching the DevTools read path to strip it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PREVIEW_SRC = REPO / "src/features/teaching_animation/ui/html_browser_preview.py"
ROUTER_SRC = REPO / "src/features/teaching_animation/render/scene_renderer_router.py"
PAGE_DECK_SRC = REPO / "src/features/teaching_animation/core/html_page_deck.py"
OVERLAY_SRC = REPO / "src/features/teaching_animation/render/html_native_overlay.py"


def _strip_selector_ids() -> set[str]:
    """DevTools Apply 前会被剥离的 DOM id 集合。

    P1-UI-01：清理清单自占位符化后不再以字面量出现在 JS 模板源码里，
    直接导入模块级 DEVTOOLS_STRIP_SELECTORS 常量校验，不再解析源码
    （对实现格式重构免疫）。
    """
    from src.features.teaching_animation.ui.html_browser_preview import (
        DEVTOOLS_STRIP_SELECTORS,
    )
    return {
        selector[1:]
        for selector in DEVTOOLS_STRIP_SELECTORS
        if selector.startswith("#")
    }


def _injected_topos_ids(*sources: Path) -> set[str]:
    """Every topos-* DOM id the host injects into the live page."""
    injected: set[str] = set()
    for path in sources:
        text = path.read_text(encoding="utf-8")
        injected.update(re.findall(r"id=\\?['\"](topos-[a-z0-9-]+)", text))
        # JS-side assignments: node.id = 'topos-...' / getElementById('...')
        # only counts as injection when it is an assignment.
        injected.update(re.findall(r"\.id\s*=\s*['\"](topos-[a-z0-9-]+)", text))
    return injected


def test_strip_list_covers_every_injected_scaffolding_id() -> None:
    stripped = _strip_selector_ids()
    injected = _injected_topos_ids(PREVIEW_SRC, ROUTER_SRC, PAGE_DECK_SRC)
    # The overlay container id is authored in the native-overlay module and
    # injected by both the live editor and the capture router.
    overlay_text = OVERLAY_SRC.read_text(encoding="utf-8")
    assert "topos-native-vector-overlay" in (ROUTER_SRC.read_text(encoding="utf-8"))
    del overlay_text
    missing = injected - stripped
    assert not missing, (
        "host-injected scaffolding ids missing from the DevTools strip list "
        f"(an Apply would bake them into the authored layer): {sorted(missing)}")


def test_native_overlay_never_enters_authored_source() -> None:
    stripped = _strip_selector_ids()
    assert "topos-native-vector-overlay" in stripped
    assert "topos-native-draw-surface" in stripped
    assert "topos-page-deck-editor-runtime" in stripped
