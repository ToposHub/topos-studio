"""R-07: map DevTools edits on native overlay nodes to scene element properties.

Applying a DevTools session used to mean one whole-HTML layer replacement:
every native (scene-owned) element got rebuilt from scratch, animations and
sibling elements included, and edits made *on* a native element inside the
overlay were silently stripped because the authored-document read excludes
the overlay subtree.

This module diffs the live overlay markup (what Chromium shows after the
user's DevTools session) against the markup the scene generates (what the
editor last installed) and translates the difference into ordinary
``UpdateElementCommand`` property edits — one undoable ``CompositeCommand``
for the whole session.  Anything outside the whitelist (unknown CSS
properties, structural DOM edits, non-numeric values) is reported as
*unmappable* so the caller can fall back to the whole-HTML replacement and
tell the user why, instead of losing the change silently.

Pure stdlib only — no Qt, no browser — so the mapping is unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# CSS declaration -> (element field, property path).  Everything not listed
# here is unmappable by design: the whitelist keeps DevTools edits from
# inventing scene state the renderer cannot reproduce.
_STYLE_MAP = {
    "left": ("transform", "x"),
    "top": ("transform", "y"),
    "width": ("transform", "width"),
    "height": ("transform", "height"),
    "opacity": ("style", "opacity"),
    "color": ("style", "color"),
    "font-size": ("style", "font_size"),
    "font-family": ("style", "font_family"),
    "font-weight": ("style", "font_weight"),
    "text-align": ("style", "text_align"),
    "line-height": ("style", "line_height"),
    "letter-spacing": ("style", "letter_spacing"),
    "background-color": ("style", "fill"),
    "background": ("style", "fill"),
    "mix-blend-mode": ("style", "blend_mode"),
    "fill-opacity": ("style", "fill_opacity"),
    "stroke-width": ("style", "stroke_width"),
}
_TRANSFORM_RE = re.compile(
    r"rotate\((-?[\d.]+)deg\)\s+scale\((-?[\d.]+),\s*(-?[\d.]+)\)")
_NUMBER_SUFFIX = ("px", "deg", "")


@dataclass(frozen=True)
class NativeEdit:
    """One whitelisted property change extracted from a DevTools session."""

    element_id: str
    field: str        # "transform" | "style" | "content"
    property: str     # e.g. "x", "opacity", "text"
    value: Any


@dataclass(frozen=True)
class DomDiffResult:
    edits: tuple[NativeEdit, ...] = ()
    unmappable: tuple[str, ...] = ()


@dataclass
class _NodeSnapshot:
    tag: str = ""
    styles: dict[str, str] = field(default_factory=dict)
    text: str = ""


class _NativeOverlayParser(HTMLParser):
    """Collect nodes carrying data-topos-native-element with style + text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: dict[str, _NodeSnapshot] = {}
        self._current: _NodeSnapshot | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        markers = {key: value for key, value in attrs if key == "data-topos-native-element"}
        if markers and self._current is None:
            self._current = _NodeSnapshot(tag=tag.lower())
            self.nodes[markers[""] if "" in markers else str(list(markers.values())[0])] = \
                self._current
            self._depth = 0
            style = dict(attrs).get("style") or ""
            self._current.styles = _parse_declarations(style)
        elif self._current is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._current is not None:
            if self._depth == 0:
                self._current = None
            else:
                self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._depth == 0:
            self._current.text += data


def _parse_declarations(inline: str) -> dict[str, str]:
    styles: dict[str, str] = {}
    for chunk in (inline or "").split(";"):
        if ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        key = key.strip().lower()
        if key:
            styles[key] = value.strip()
    return styles


def _as_float(value: str) -> float | None:
    text = str(value).strip().lower()
    for suffix in _NUMBER_SUFFIX:
        if text.endswith(suffix):
            text = text[: len(text) - len(suffix)].strip()
            break
    try:
        return float(text)
    except ValueError:
        return None


def _color_key(value: str) -> str:
    """Normalize hex/rgb colors so 32.0000px-style roundtrips compare equal."""
    text = str(value).strip().lower().replace(" ", "")
    if text.startswith("#") and len(text) == 7:
        return text
    rgb = re.fullmatch(r"rgb\((\d+),(\d+),(\d+)\)", text)
    if rgb:
        return "#{:02x}{:02x}{:02x}".format(*(int(group) for group in rgb.groups()))
    return text


def _values_equal(key: str, before: str, after: str) -> bool:
    if key in {"color", "background-color", "background", "fill", "background"}:
        return _color_key(before) == _color_key(after)
    if key in {"font-family", "font-weight", "text-align", "mix-blend-mode"}:
        return before.strip().lower() == after.strip().lower()
    before_number = _as_float(before)
    after_number = _as_float(after)
    if before_number is not None and after_number is not None:
        return abs(before_number - after_number) <= 1e-3
    return before.strip() == after.strip()


def extract_native_nodes(html: str) -> dict[str, _NodeSnapshot]:
    parser = _NativeOverlayParser()
    parser.feed(html or "")
    parser.close()
    return parser.nodes


def diff_native_dom(before_html: str, after_html: str) -> DomDiffResult:
    """Diff two overlay markups into whitelisted edits + unmappable reasons."""
    before = extract_native_nodes(before_html)
    after = extract_native_nodes(after_html)
    edits: list[NativeEdit] = []
    unmappable: list[str] = []

    for element_id in sorted(set(before) - set(after)):
        unmappable.append(f"native element {element_id!r} was removed in the live DOM")
    for element_id in sorted(set(after) - set(before)):
        unmappable.append(f"native element {element_id!r} was added in the live DOM")

    for element_id in sorted(set(before) & set(after)):
        old, new = before[element_id], after[element_id]
        if old.tag != new.tag:
            unmappable.append(f"{element_id}: tag changed {old.tag} → {new.tag}")
            continue
        transform_edits: dict[str, float] = {}
        style_edits: dict[str, Any] = {}
        for key in sorted(set(old.styles) | set(new.styles)):
            old_value = old.styles.get(key, "")
            new_value = new.styles.get(key, "")
            if _values_equal(key, old_value, new_value):
                continue
            if key == "transform":
                match = _TRANSFORM_RE.fullmatch(new_value.strip())
                if match is None:
                    unmappable.append(
                        f"{element_id}: unsupported transform {new_value!r}")
                    continue
                rotation, scale_x, scale_y = (float(g) for g in match.groups())
                transform_edits.update(
                    rotation=rotation, scale_x=scale_x, scale_y=scale_y)
                continue
            mapping = _STYLE_MAP.get(key)
            if mapping is None:
                unmappable.append(f"{element_id}: unmapped CSS property {key!r} changed")
                continue
            if mapping[0] == "transform":
                number = _as_float(new_value)
                if number is None:
                    unmappable.append(f"{element_id}: {key} value is not numeric: {new_value!r}")
                    continue
                transform_edits[mapping[1]] = number
            elif key in {"color", "background-color", "background"}:
                style_edits[mapping[1]] = _color_key(new_value)
            elif key in {"font-family", "font-weight", "text-align", "mix-blend-mode"}:
                style_edits[mapping[1]] = new_value.strip()
            else:
                number = _as_float(new_value)
                if number is None:
                    unmappable.append(f"{element_id}: {key} value is not numeric: {new_value!r}")
                    continue
                style_edits[mapping[1]] = number
        if old.text.strip() != new.text.strip():
            if new.tag in {"div", "span"}:  # text/formula elements render as divs
                edits.append(NativeEdit(element_id, "content", "text", new.text.strip()))
            else:
                unmappable.append(f"{element_id}: text content is not editable on <{new.tag}>")
        for name, value in transform_edits.items():
            edits.append(NativeEdit(element_id, "transform", name, value))
        for name, value in style_edits.items():
            edits.append(NativeEdit(element_id, "style", name, value))
    return DomDiffResult(tuple(edits), tuple(unmappable))


def edits_to_update_commands(
    scene: Any, edits: tuple[NativeEdit, ...]
) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    """Fold edits per element into one UpdateElementCommand each.

    Returns ``(commands, unmappable)``; unknown element ids are unmappable
    rather than exceptions so the caller can fall back with a reason.
    """
    from .commands import UpdateElementCommand

    by_element: dict[str, dict[str, Any]] = {}
    unmappable: list[str] = []
    for edit in edits:
        element = scene.element_by_id(edit.element_id)
        if element is None:
            unmappable.append(f"no scene element with id {edit.element_id!r}")
            continue
        bucket = by_element.setdefault(edit.element_id, {})
        if edit.field == "transform":
            transform = bucket.get("transform") or {
                key: getattr(element.transform, key) for key in
                ("x", "y", "width", "height", "rotation", "scale_x", "scale_y")}
            transform[edit.property] = edit.value
            bucket["transform"] = transform
        elif edit.field == "style":
            style = bucket.get("style")
            if style is None:
                style = dict(element.style)
                bucket["style"] = style
            style[edit.property] = edit.value
        else:  # content
            content = bucket.get("content")
            if content is None:
                content = dict(element.content) if isinstance(element.content, dict) else {}
                bucket["content"] = content
            content[edit.property] = edit.value
    commands = tuple(
        UpdateElementCommand(element_id, updates, label="DevTools Property Edit")
        for element_id, updates in sorted(by_element.items()))
    return commands, tuple(unmappable)


__all__ = [
    "DomDiffResult", "NativeEdit", "diff_native_dom",
    "edits_to_update_commands", "extract_native_nodes",
]
