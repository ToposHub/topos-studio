"""FH-10 apply half: turn a validated ``HtmlPatch`` into a new HTML layer.

``validate_patch`` (html_patch.py) is the fail-before-write boundary; this
module is the *apply*: it takes a patch + the current ``FreeformHtmlMotionLayer``,
mutates only the patch's declared target, **re-sanitizes** the result (defence
in depth — an edited page must pass the allowlist again before it ships), and
returns a new layer with a bumped revision and re-hashed source.  There is no
generic ``innerHTML``/``stylesheet`` write: each operation acts on one declared
node/token/slot only, so an edit can never partially write or bypass the
sanitizer.

Node location is done with a stack-based tag scanner over the sanitized HTML
(no third-party DOM dependency — the sanitizer itself uses stdlib ``html.parser``,
and ``lxml`` is not a declared project dependency).  The scanner honours
nesting, so a target with same-tag descendants resolves to the correct closing
tag; void elements (``img``/``br``/…) have no inner content.  Self-closing
syntax (``<div … />``) is recognized as immediately closed — its inner region
is empty, never "the rest of the document" — and the SVG geometry tags the
sanitizer re-emits self-closing (``<rect/>``/``<path/>``/…) locate like void
elements.  A tag with no findable closing tag is reported as unlocatable
rather than silently swallowing the remainder of the document.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from ...core.html_layer import FreeformHtmlMotionLayer
from .html_patch import (
    HtmlLayerState,
    HtmlPatch,
    HtmlPatchError,
    HtmlPatchErrorCode,
    HtmlPatchOperation,
    validate_patch,
)
from .node_identity import extract_declared_nodes, extract_slots, extract_tokens
from .sanitizer import HtmlSanitizer


# Void HTML elements have no closing tag and no inner content.
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
# SVG geometry the sanitizer re-emits self-closing (see sanitizer's
# handle_starttag/handle_startendtag): these never carry inner content, so
# they must locate like void elements instead of hunting a ``</rect>`` that
# the sanitizer deliberately does not emit.
_SELF_CLOSING_SVG_ELEMENTS = frozenset({
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "stop", "use", "fegaussianblur", "fedropshadow",
})
# Tags that can never have an inner region, whatever syntax they were written in.
_EMPTY_INNER_ELEMENTS = _VOID_ELEMENTS | _SELF_CLOSING_SVG_ELEMENTS
_TAG_RE = re.compile(
    r"<(/?)([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
    re.DOTALL,
)
_ATTR_RE = re.compile(r'([a-zA-Z_:][\w:.\-]*)(?:\s*=\s*"([^"]*)"|\s*=\s*\'([^\']*)\'|\s*=\s*([^\s>]+))?')
_TOKEN_DECL_RE = r"(--%s\s*:[\s]*)([^;}\n]+)"
_NODE_ID_ATTR = "data-topos-node-id"
_TOPOS_ASSET = "topos-asset://"


# --------------------------------------------------------------------------- #
# Tag scanning / node location
# --------------------------------------------------------------------------- #

class _ElementSpan:
    """Byte spans of a located element within the source HTML."""

    __slots__ = ("tag", "open_start", "open_end", "inner_start", "inner_end",
                 "close_end", "is_void", "self_closed")

    def __init__(self, tag: str, open_start: int, open_end: int,
                 inner_start: int, inner_end: int, close_end: int, is_void: bool,
                 self_closed: bool = False) -> None:
        self.tag = tag
        self.open_start = open_start   # index of '<'
        self.open_end = open_end       # index after the opening '>' (or '/>')
        self.inner_start = inner_start
        self.inner_end = inner_end
        self.close_end = close_end     # index after the closing '</tag>' (== open_end for void/self-closed)
        self.is_void = is_void
        self.self_closed = self_closed  # syntactically self-closed open tag (<div … />)


def _parse_attrs(attrs_str: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for name, dq, sq, bare in _ATTR_RE.findall(attrs_str):
        attrs[name] = dq if dq is not None else (sq if sq is not None else (bare if bare is not None else ""))
    return attrs


def _is_self_closing_syntax(attrs_str: str) -> bool:
    """True when a start tag's tail ends with ``/`` (``<div … />``).

    Only the tail is inspected, so a quoted value ending in ``/``
    (``href="a/"``) is not misread.  The sanitizer always quotes attribute
    values, which keeps the pathological unquoted ``foo/`` form out of
    sanitized sources.
    """
    return attrs_str.rstrip().endswith("/")


def _locate_element(html: str, node_id: str) -> _ElementSpan | None:
    """Find the element carrying ``data-topos-node-id="node_id"``.

    Stack-based locator: find the open tag with the node id, then its match.
    Walks every tag with a stack so a target nested inside same-tag descendants
    resolves to its own closing tag.  Returns None if not found.

    Self-closing syntax is honoured both ways: a self-closed target (or any
    tag that can never carry inner content — void elements plus the SVG
    geometry the sanitizer re-emits self-closing) yields an *empty* inner
    region, and a self-closed same-tag descendant never increments depth.
    A start tag with no findable closing tag is malformed: None is returned
    so callers raise a diagnosable error instead of treating the remainder of
    the document as inner content.
    """
    matches = list(_TAG_RE.finditer(html))
    # First, find the index of the target open tag.  Void elements (img/br/…)
    # are valid targets too — they carry node ids for asset slots — so only
    # closing tags are skipped here.  Voidness is honoured below (is_void span)
    # and in the depth walk (void tags never increment depth).
    target_idx = -1
    target_tag = ""
    for idx, m in enumerate(matches):
        slash, tag, attrs_str = m.group(1), m.group(2), m.group(3)
        if slash == "/":
            continue
        if _parse_attrs(attrs_str).get(_NODE_ID_ATTR) == node_id:
            target_idx = idx
            target_tag = tag
            break
    if target_idx < 0:
        return None
    open_match = matches[target_idx]
    open_start, open_end = open_match.start(), open_match.end()
    tag_l = target_tag.lower()
    is_empty_inner = tag_l in _EMPTY_INNER_ELEMENTS
    self_closed = _is_self_closing_syntax(open_match.group(3))
    # Immediately-closed forms: void/SVG-geometry tags in any syntax, and any
    # tag written self-closed (<div … />).  Inner is empty — never the rest of
    # the document.
    if is_empty_inner or self_closed:
        return _ElementSpan(target_tag, open_start, open_end, open_end, open_end, open_end,
                            is_empty_inner, self_closed=self_closed and not is_empty_inner)
    # Walk forward maintaining a stack of same-tag opens to find the matching close.
    depth = 1
    for m in matches[target_idx + 1:]:
        slash, tag = m.group(1), m.group(2)
        if tag.lower() != tag_l:
            continue
        if slash == "/":
            depth -= 1
            if depth == 0:
                close_end = m.end()
                return _ElementSpan(target_tag, open_start, open_end, open_end, m.start(), close_end, False)
        elif tag.lower() not in _EMPTY_INNER_ELEMENTS and not _is_self_closing_syntax(m.group(3)):
            # A same-tag descendant that is self-closed or can never hold
            # content does not consume a closing tag: skip it.
            depth += 1
    # No matching close tag found (malformed, unterminated source): report the
    # node as unlocatable.  Splicing into "the rest of the document" here is
    # what made replace_text wipe everything after the target.
    return None


# --------------------------------------------------------------------------- #
# Public read helpers (used by template promotion; generally useful)
# --------------------------------------------------------------------------- #

def read_node_inner_text(html: str, node_id: str) -> str | None:
    """Return the inner text of the element carrying ``data-topos-node-id``.

    Returns ``None`` when the node is absent (or unterminated/malformed), and
    ``""`` for void elements and self-closed tags (which have no inner
    content).  Used by template promotion to capture a content slot's current
    value.
    """
    span = _locate_element(html, node_id)
    if span is None:
        return None
    if span.is_void:
        return ""
    return html[span.inner_start:span.inner_end].strip()


def read_node_asset_ref(html: str, node_id: str) -> str | None:
    """Return the asset id referenced by the node's ``topos-asset://`` ref.

    Looks at the node's ``src`` (falling back to ``href``) attribute, strips
    the ``topos-asset://`` scheme, and returns the bare id.  Returns ``None``
    when the node is absent or carries no topos-asset reference.
    """
    span = _locate_element(html, node_id)
    if span is None:
        return None
    open_tag = html[span.open_start:span.open_end]
    m = _TAG_RE.match(open_tag)
    attrs = _parse_attrs(m.group(3)) if m else {}
    ref = (attrs.get("src") or attrs.get("href") or "").strip()
    if ref.lower().startswith(_TOPOS_ASSET):
        return ref[len(_TOPOS_ASSET):]
    return None


def _set_attr_on_open_tag(open_tag: str, name: str, value: str) -> str:
    """Set/replace an attribute on a full opening-tag string (``<tag ...>``)."""
    pattern = re.compile(rf'(\s{re.escape(name)}\s*=\s*)"([^"]*)"')
    if pattern.search(open_tag):
        return pattern.sub(lambda m: f'{m.group(1)}"{value}"', open_tag, count=1)
    # Not present: insert before the closing '>' (or '/>' for self-closing).
    if open_tag.rstrip().endswith("/>"):
        return open_tag.rstrip()[:-2].rstrip() + f' {name}="{value}"/>'
    return open_tag.rstrip()[:-1].rstrip() + f' {name}="{value}">'


# --------------------------------------------------------------------------- #
# Operation handlers — each returns the new HTML string
# --------------------------------------------------------------------------- #

def _op_replace_text(html: str, patch: HtmlPatch, state: HtmlLayerState) -> str:
    span = _locate_element(html, patch.target)
    if span is None:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_NODE, f"node {patch.target} not in source")
    if span.is_void:
        raise HtmlPatchError(HtmlPatchErrorCode.EDIT_MODE_FORBIDS,
                             f"{patch.target} is a void element; cannot set text")
    if span.self_closed:
        # A syntactically self-closed element (<div … />) has no inner region
        # to splice into.  Expand it to an explicitly paired tag so the new
        # text becomes its content rather than a trailing sibling (and never
        # the "rest of the document" the malformed fallback used to claim).
        open_tag = html[span.open_start:span.open_end].rstrip()
        expanded_open = open_tag[:-2].rstrip() + ">"
        return (html[:span.open_start] + expanded_open + str(patch.value)
                + f"</{span.tag}>" + html[span.open_end:])
    # Replace the entire inner content with the new text (content-mode nodes are
    # simple text containers). The re-sanitize step validates the result.
    return html[:span.inner_start] + str(patch.value) + html[span.inner_end:]


def _op_set_asset(html: str, patch: HtmlPatch, state: HtmlLayerState) -> str:
    span = _locate_element(html, patch.target)
    if span is None:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_NODE, f"node {patch.target} not in source")
    open_tag = html[span.open_start:span.open_end]
    attr = "src" if "src" in open_tag else "href"
    new_open = _set_attr_on_open_tag(open_tag, attr, f"topos-asset://{patch.value}")
    return html[:span.open_start] + new_open + html[span.open_end:]


def _op_transform_node(html: str, patch: HtmlPatch, state: HtmlLayerState) -> str:
    span = _locate_element(html, patch.target)
    if span is None:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_NODE, f"node {patch.target} not in source")
    value = patch.value or {}
    parts = []
    if "x" in value or "y" in value:
        parts.append(f"translate({value.get('x', 0)}, {value.get('y', 0)})")
    if "rotation" in value:
        parts.append(f"rotate({value['rotation']}deg)")
    if "scale" in value:
        parts.append(f"scale({value['scale']})")
    transform = " ".join(parts)
    open_tag = html[span.open_start:span.open_end]
    # Merge into any existing inline style transform.
    style_re = re.compile(r'(style\s*=\s*")([^"]*)(")')
    if style_re.search(open_tag):
        def _merge(m: re.Match) -> str:
            existing = m.group(2)
            existing = re.sub(r"transform\s*:[^;\"']*;?", "", existing).rstrip()
            merged = (existing + ("; " if existing and not existing.endswith(";") else "")).lstrip()
            return f'{m.group(1)}{merged}transform: {transform}{m.group(3)}'
        new_open = style_re.sub(_merge, open_tag, count=1)
    else:
        new_open = _set_attr_on_open_tag(open_tag, "style", f"transform: {transform}")
    return html[:span.open_start] + new_open + html[span.open_end:]


def _op_set_slot_attr(html: str, patch: HtmlPatch, state: HtmlLayerState) -> str:
    # AUD-30011: locate the slot via the tag scanner, never a whole-file string
    # find.  A CSS attribute selector inside <style> —
    # ``[data-topos-editable="x"] { … }`` — sits earlier in the document and a
    # plain ``find`` hit it first, splicing the attribute into the stylesheet
    # instead of the element.  The tag walk only sees real tags, and every
    # element carrying the slot id is updated (the old code silently changed
    # only the first occurrence).
    pieces: list[str] = []
    consumed = 0
    found = False
    for m in _TAG_RE.finditer(html):
        if m.group(1) == "/":
            continue
        attrs = _parse_attrs(m.group(3))
        if attrs.get("data-topos-editable") != patch.target:
            continue
        new_open = _set_attr_on_open_tag(m.group(0), patch.sub_target, str(patch.value))
        pieces.append(html[consumed:m.start()])
        pieces.append(new_open)
        consumed = m.end()
        found = True
    if not found:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_SLOT, f"slot {patch.target} not in source")
    pieces.append(html[consumed:])
    return "".join(pieces)


def _op_set_token(html: str, patch: HtmlPatch, state: HtmlLayerState) -> str:
    pattern = re.compile(_TOKEN_DECL_RE % re.escape(patch.target))
    new, n = pattern.subn(lambda m: f"{m.group(1)}{patch.value}", html, count=1)
    if n == 0:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_TOKEN, f"token --{patch.target} not in source")
    return new


# One CSS time token (``2s`` / ``0.5s`` / ``500ms`` / ``.5s``) inside an
# ``animation`` shorthand.  Digits alone (``steps(2, end)``, easing curves)
# never match, so only genuine durations/delays are candidates.
_CSS_TIME_RE = re.compile(r"(?<![\w.-])(\d*\.?\d+)(ms|s)(?![\w-])", re.IGNORECASE)


def _replace_animation_duration(prop: str, value: str, seconds: float) -> str:
    """Rewrite only the duration component of an ``animation`` declaration.

    ``animation-duration`` is a long property: the whole value is the duration.
    The ``animation`` shorthand carries the name/iteration-count/easing/delay
    besides its *first* time token (the duration; a second token is the delay),
    so only that first time token may change — the previous whole-value
    rewrite turned ``animation: slide 2s ease-in infinite`` into
    ``animation: 0.5s``, silently deleting the animation (AUD-30009).
    """
    if prop.lower().endswith("-duration"):
        return f"{seconds:g}s"
    replaced = {"done": False}

    def _swap(match: re.Match) -> str:
        if replaced["done"]:
            return match.group(0)
        replaced["done"] = True
        return f"{seconds:g}s"

    new_value = _CSS_TIME_RE.sub(_swap, value)
    if not replaced["done"]:
        # Shorthand without any time component (duration defaulted to 0s):
        # append the duration — the shorthand grammar accepts it anywhere.
        return f"{value.strip()} {seconds:g}s"
    return new_value


def _op_set_animation_timing(html: str, patch: HtmlPatch, state: HtmlLayerState) -> str:
    # validate_patch guarantees a non-negative int duration_ms (AUD-30006);
    # the int() here therefore never sees "abc"/[1]/negative input.
    duration_ms = int(patch.value["duration_ms"])
    seconds = duration_ms / 1000.0
    span = _locate_element(html, patch.target)
    if span is None:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_NODE, f"node {patch.target} not in source")
    # The node's animation lives in a CSS rule keyed by its node id (the AI
    # authoring convention is [data-topos-node-id="ID"] { animation: ... }).
    # The property name is captured so the shorthand and the long form can be
    # rewritten with the right granularity (see _replace_animation_duration).
    rule_re = re.compile(
        rf'(\[data-topos-node-id\s*=\s*"{re.escape(patch.target)}"[^{{]*\{{[^}}]*?(animation(?:-duration)?)\s*:\s*)([^;}}]+)([;}}])',
        re.DOTALL,
    )
    new, n = rule_re.subn(
        lambda m: f"{m.group(1)}{_replace_animation_duration(m.group(2), m.group(3), seconds)}{m.group(4)}",
        html, count=1)
    if n == 0:
        # Fall back to the inline style on the node itself.
        open_tag = html[span.open_start:span.open_end]
        dur_re = re.compile(r'(animation(?:-duration)?)\s*:\s*([^;"\']+)')
        if dur_re.search(open_tag):
            new_open = dur_re.sub(
                lambda m: f"{m.group(1)}:{_replace_animation_duration(m.group(1), m.group(2), seconds)}",
                open_tag, count=1)
            return html[:span.open_start] + new_open + html[span.open_end:]
        raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE,
                             f"no animation timing found for node {patch.target}")
    return new


_OPS = {
    HtmlPatchOperation.REPLACE_TEXT: _op_replace_text,
    HtmlPatchOperation.SET_ASSET: _op_set_asset,
    HtmlPatchOperation.TRANSFORM_NODE: _op_transform_node,
    HtmlPatchOperation.SET_SLOT_ATTR: _op_set_slot_attr,
    HtmlPatchOperation.SET_TOKEN: _op_set_token,
    HtmlPatchOperation.SET_ANIMATION_TIMING: _op_set_animation_timing,
}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def _layer_state(layer: FreeformHtmlMotionLayer) -> HtmlLayerState:
    """Build the validation state from a layer's current source."""
    nodes = extract_declared_nodes(layer.sanitized_html)
    return HtmlLayerState(
        revision=layer.revision,
        source_hash=layer.source_hash,
        declared_nodes=nodes,
        declared_tokens=extract_tokens(layer.sanitized_html),
        declared_slots=extract_slots(layer.sanitized_html),
        bound_asset_ids=frozenset(layer.bound_asset_ids),
    )


def apply_html_patch(layer: FreeformHtmlMotionLayer, patch: HtmlPatch, *,
                     sanitizer: HtmlSanitizer | None = None) -> FreeformHtmlMotionLayer:
    """Validate + apply a patch, returning a new layer (bumped revision, re-sanitized).

    Raises :class:`HtmlPatchError` if the patch fails validation or targets a
    node/token/slot not present in the layer's source.  The result is always
    re-run through the allowlist sanitizer so an edit can never ship content the
    sanitizer would have stripped.
    """
    state = _layer_state(layer)
    validate_patch(patch, state)
    handler = _OPS.get(patch.operation)
    if handler is None:  # pragma: no cover - validate_patch guards this
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_OPERATION, str(patch.operation))
    new_html = handler(layer.sanitized_html, patch, state)
    if sanitizer is not None:
        san = sanitizer
    else:
        # A Motion Graphic layer is an explicitly opted-in browser document.
        # Re-sanitizing an edit with the generic offline profile would
        # silently remove the very JS/Canvas/CSS capabilities that survived
        # generation. Generic imported HTML keeps the conservative defaults.
        metadata = getattr(layer, "metadata", {}) or {}
        san = HtmlSanitizer(
            allow_network_resources=metadata.get("network_access", False) is True,
            allow_inline_scripts=metadata.get("allow_inline_scripts", False) is True,
            allow_event_handlers=metadata.get("allow_event_handlers", False) is True,
            allow_freeform_css=metadata.get("allow_freeform_css", False) is True,
            allow_inline_visual_resources=metadata.get(
                "allow_inline_visual_resources", False,
            ) is True,
        )
    sanitized = san.sanitize(new_html)
    if not sanitized.html.strip():
        raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE,
                             "patch produced empty HTML after sanitization")
    # AUD-30010 / FH-01 contract: ``source_hash`` is the authored
    # (pre-sanitization) source — here the handler output, exactly the text
    # fed into the sanitizer — while ``sanitizer_hash`` covers the sanitized
    # result.  Writing the sanitized digest into both fields collapsed the
    # audit trail (source_hash == sanitizer_hash after any patch, so a
    # reviewer could no longer see what the sanitizer changed) and computed
    # the same sha256 twice.
    new_source_hash = hashlib.sha256(new_html.encode("utf-8")).hexdigest()
    new_sanitizer_hash = hashlib.sha256(sanitized.html.encode("utf-8")).hexdigest()
    return replace(
        layer,
        sanitized_html=sanitized.html,
        source_hash=new_source_hash,
        sanitizer_hash=new_sanitizer_hash,
        revision=layer.revision + 1,
    )


__all__ = ["apply_html_patch", "read_node_inner_text", "read_node_asset_ref"]
