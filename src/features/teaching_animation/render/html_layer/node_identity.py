"""FH-13: HTML node identity + edit-mode contract.

The sanitizer keeps ``data-topos-node-id`` and ``data-topos-edit-mode`` on
authored nodes.  This module walks sanitized HTML to extract the addressable
nodes (the things the canvas/inspector may select and a ``HtmlPatch`` may
target) and their edit capabilities.

Edit modes (FH-13):
* ``content``      — text/asset/token edits only; no free transform (Flex/Grid child).
* ``absolute_transform`` — may be moved/scaled/rotated on the canvas.
* ``token_only``   — only CSS-token edits, no content/transform.

Nodes without ``data-topos-node-id`` are NOT individually addressable: the canvas
selects their parent HTML layer as a whole (UX-09).  This is the rule that keeps
responsive Flex/Grid layouts from being broken by ad-hoc drag.
"""

from __future__ import annotations

from html.parser import HTMLParser

from .html_patch import DeclaredNode


_NODE_ID_ATTR = "data-topos-node-id"
_EDIT_MODE_ATTR = "data-topos-edit-mode"
_EDITABLE_ATTR = "data-topos-editable"
_ASSET_TAGS = frozenset({"img", "picture", "source"})


class _NodeScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: dict[str, dict] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        node_id = attr_map.get(_NODE_ID_ATTR)
        if not node_id:
            return
        edit_mode = attr_map.get(_EDIT_MODE_ATTR, "content")
        if edit_mode not in {"content", "absolute_transform", "token_only"}:
            edit_mode = "content"
        existing = self.nodes.get(node_id)
        # An asset slot if the tag is an image-like tag OR it carries an editable=asset marker.
        is_asset = tag.lower() in _ASSET_TAGS or attr_map.get(_EDITABLE_ATTR) == "asset"
        if existing:
            # Merge: once an asset, stays an asset; strictest edit_mode wins.
            existing["edit_mode"] = _stricter_mode(existing["edit_mode"], edit_mode)
            existing["asset_slot"] = existing["asset_slot"] or is_asset
        else:
            self.nodes[node_id] = {"edit_mode": edit_mode, "asset_slot": is_asset, "tag": tag.lower()}


def _stricter_mode(a: str, b: str) -> str:
    order = {"token_only": 0, "content": 1, "absolute_transform": 2}
    return a if order.get(a, 1) < order.get(b, 1) else b


def extract_declared_nodes(sanitized_html: str) -> dict[str, DeclaredNode]:
    """Return the addressable nodes declared in sanitized HTML."""
    scanner = _NodeScanner()
    try:
        scanner.feed(sanitized_html)
        scanner.close()
    except Exception:
        return {}
    return {
        node_id: DeclaredNode(node_id=node_id, edit_mode=data["edit_mode"], asset_slot=data["asset_slot"])
        for node_id, data in scanner.nodes.items()
    }


def extract_tokens(sanitized_html: str) -> frozenset[str]:
    """Find CSS custom property definitions (``--token:``) the layer exposes."""
    import re
    matches = re.findall(r"--([A-Za-z0-9_-]+)\s*:", sanitized_html)
    return frozenset(matches)


def extract_slots(sanitized_html: str) -> frozenset[str]:
    """Find ``data-topos-editable`` slot ids."""
    import re
    matches = re.findall(r'data-topos-editable="([^"]+)"', sanitized_html)
    return frozenset(matches)


__all__ = ["extract_declared_nodes", "extract_slots", "extract_tokens"]
