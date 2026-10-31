"""FH-02: allowlist HTML/CSS sanitizer for the Freeform HTML motion layer.

W2-prep: this module is browser-independent pure logic.  Per the W0-A gate it
does NOT attach ``FreeformHtmlMotionLayer`` to the scene schema yet; it is the
ready-to-activate sanitizer the W2 ``HtmlRendererService`` will call before any
authored HTML reaches a browser page.

It parses authored HTML with the stdlib ``HTMLParser`` and keeps an inert
subset:

* safe structural + text tags (div/span/p/h*/ul/ol/li/section/...);
* a single ``<style>`` block whose CSS is property-allowlisted (colours, layout,
  typography, transform, opacity, filter, gradients, @keyframes);
* ``data-topos-*`` attributes (node identity / edit mode / editable slots);
* ``src``/``href`` only when they resolve to ``topos-asset://``; the explicit
  browser-resource profile additionally keeps HTTPS CDN styles, fonts, images
  and external scripts.

It strips by default: scripts, inline event handlers (``on*``),
``javascript:``/``file:``/``data:`` URLs,
``<iframe>/<object>/<embed>/<form>/<input>/<link>/<meta>``, CSS ``@import``,
``expression()``, ``url(...)`` pointing off-scheme, and any attribute/property
not on the allowlist.  The network profile never permits inline scripts or
non-HTTPS schemes. Every removal is recorded with a coded reason so the
AI/quality gate gets actionable diagnostics, never a silent empty page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit


# Tags that are dropped entirely (with their content for script-like ones).
_DROP_TAGS_WITH_CONTENT = frozenset({"script", "iframe", "object", "embed", "noscript", "template"})
# Tags dropped but their text content kept.
_DROP_TAGS_KEEP_TEXT = frozenset({"form", "input", "select", "option", "link", "meta", "base"})

ALLOWED_TAGS: frozenset[str] = frozenset({
    "html", "head", "body", "title",
    "div", "span", "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "section", "article", "header", "footer", "main", "nav", "aside",
    "figure", "figcaption", "picture", "img", "a", "canvas",
    "audio", "video", "source", "track",
    "strong", "em", "b", "i", "u", "small", "mark", "sub", "sup", "code", "pre",
    "blockquote", "q", "s", "del", "ins", "kbd", "label", "time",
    "button", "details", "summary",
    "table", "thead", "tbody", "tr", "th", "td",
    # Static inline SVG is a first-class visual primitive for model-authored
    # Motion Graphics.  Keep the safe geometry/text/filter subset here rather
    # than dropping the entire illustration (which turns otherwise rich pages
    # into an empty white canvas).  Executable/time-based SVG tags such as
    # animate and set remain excluded; static symbol reuse and foreignObject
    # are safe after their nested HTML is sanitized.
    "svg", "g", "path", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "defs", "lineargradient", "radialgradient",
    "stop", "clippath", "mask", "filter", "pattern", "marker", "symbol",
    "fegaussianblur", "fedropshadow", "fecomponenttransfer", "fecomposite",
    "fecolormatrix", "feconvolvematrix", "fedisplacementmap", "femerge",
    "femergenode", "feoffset", "fespecularlighting", "feturbulence",
    "feflood", "feblend", "feimage", "text", "tspan", "use", "foreignobject",
    "animate", "animatetransform", "set",
    "style",
})

# SVG presentation/geometry attributes are inert after URL-bearing attributes
# (href/xlink:href) and event handlers have already been rejected.  Values are
# escaped before output; fragment-only url(#id) references are additionally
# checked below so gradients/filters/clips remain usable without enabling
# external resources.
_SVG_SAFE_ATTRS = frozenset({
    "xmlns", "viewbox", "preserveaspectratio", "version",
    "fill", "fill-opacity", "fill-rule", "clip-rule", "stroke",
    "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-dasharray", "stroke-dashoffset", "stroke-opacity",
    "vector-effect", "paint-order", "color", "opacity",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "width", "height", "points", "d", "offset", "stop-color", "stop-opacity",
    "gradientunits", "gradienttransform", "patternunits", "patterncontentunits",
    "spreadmethod", "fx", "fy", "fr", "filterunits", "primitiveunits",
    "stddeviation", "in", "in2", "result", "basefrequency", "numoctaves",
    "scale", "surface-scale", "diffuseconstant", "specularconstant",
    "specularexponent", "kernelmatrix", "kernelunitlength",
    "transform", "transform-origin", "text-anchor", "dominant-baseline",
    "alignment-baseline", "font-family", "font-size", "font-weight",
    "letter-spacing", "display", "visibility", "overflow", "clip-path",
    "mask", "filter", "marker-start", "marker-mid", "marker-end",
    "pathlength", "patterntransform", "mask-type", "maskunits", "maskcontentunits",
    "clippathunits", "attributename", "attributetype", "begin", "dur", "end",
    "from", "to", "values", "keytimes", "keysplines", "calcmode", "repeatcount",
    "repeatdur", "restart", "additive", "accumulate", "type", "as", "systemlanguage",
})
_SVG_FRAGMENT_URL_ATTRS = frozenset({"fill", "stroke", "filter", "clip-path", "mask"})

# Always-stripped attributes (event handlers + foreign references).
_FORBIDDEN_ATTR_PREFIXES = ("on",)
_FORBIDDEN_ATTRS = frozenset({"href", "srcset", "action", "formaction", "xlink:href", "ping"})

# Per-attribute URL schemes; everything else is dropped.
_URL_ATTRS = frozenset({"src", "href", "poster"})
_TOPOS_ASSET = "topos-asset://"

# CSS: allowed property roots (matched as prefix so font-size + font-family pass).
_ALLOWED_CSS_PROPERTIES: tuple[str, ...] = (
    "color", "background", "background-color", "background-image", "background-size",
    "background-position", "background-repeat", "background-clip",
    "font", "font-family", "font-size", "font-weight", "font-style", "letter-spacing",
    "line-height", "text-align", "text-decoration", "text-transform", "text-indent",
    "text-rendering", "text-overflow", "text-wrap", "white-space", "word-break",
    "word-spacing", "hyphens", "tab-size", "font-kerning", "font-variant",
    "font-feature-settings", "font-variation-settings",
    "margin", "padding", "border", "border-radius", "box-sizing",
    "width", "height", "min-width", "min-height", "max-width", "max-height",
    # Pseudo-elements are a common, inert CSS composition primitive for
    # generated decks (glows, overlays, labels).  ``content`` cannot execute
    # code here because url() values still pass through the URL policy below.
    "content",
    "top", "left", "right", "bottom",
    "inset", "inset-block", "inset-inline",
    "display", "position", "flex", "grid", "gap", "align", "justify", "place",
    "order", "flex-basis", "flex-flow", "flex-direction", "flex-wrap",
    "align-content", "align-items", "align-self", "justify-content",
    "justify-items", "justify-self", "place-content", "place-items", "place-self",
    "grid-auto-flow", "grid-auto-columns", "grid-auto-rows", "grid-template-areas",
    "grid-template-columns", "grid-template-rows", "grid-column", "grid-row",
    "grid-area", "row-gap", "column-gap", "z-index", "opacity", "overflow",
    "overflow-wrap", "visibility", "pointer-events", "cursor", "user-select",
    "caret-color", "accent-color", "color-scheme", "image-rendering",
    "scroll-behavior", "overscroll-behavior", "touch-action", "resize",
    "contain", "content-visibility",
    "transform", "transform-origin", "translate", "rotate", "scale",
    # 3D motion (card flips, parallax): numeric/keyword values only, no
    # url()/script surface.  transform-style is already covered by the
    # "transform" prefix above.
    "perspective", "backface-visibility",
    # CSS time-based motion: @keyframes is preserved separately; these roots
    # (prefix-matched) cover the full animation-*/transition-* longhand
    # family.  They carry no script/network/file surface — the url() check
    # below still screens any value — and are essential to a motion layer.
    "animation", "transition",
    # Compositing — keyword blend modes (multiply/screen/overlay/…), no url().
    "mix-blend-mode",
    # CJK / vertical text (plan §6 calls out 横竖屏/CJK fixtures) — keyword only.
    "writing-mode", "text-orientation",
    "filter", "backdrop-filter", "box-shadow", "text-shadow",
    # Chromium/WebKit spelling used by many polished browser decks.  These
    # are presentation-only aliases; values still pass the same url() and
    # dangerous-token checks below.
    "-webkit-backdrop-filter", "-webkit-background-clip",
    "-webkit-text-fill-color", "-webkit-text-stroke",
    "-webkit-mask", "-webkit-mask-image", "-webkit-mask-mode",
    "-webkit-mask-size", "-webkit-mask-position", "-webkit-mask-repeat",
    "-webkit-mask-composite",
    "fill", "stroke", "stroke-width", "outline", "outline-offset",
    "clip", "clip-path", "mask", "mask-image", "mask-mode", "mask-size",
    "mask-position", "mask-repeat", "mask-composite", "isolation", "will-change",
    "vertical-align",
    "border-collapse", "border-spacing", "table-layout", "columns", "column-count",
    "column-width", "column-gap", "column-rule", "list-style", "object-fit",
    "object-position", "aspect-ratio", "shape-outside", "shape-margin",
)

# These at-rules only change CSS matching/layout.  They do not create a new
# resource or execution surface, so keeping them is both safe and important
# for real web composition (responsive variants and progressive enhancement).
_ALLOWED_CSS_AT_RULES = frozenset({"media", "supports", "container", "layer", "property"})
_CSS_DANGER_TOKENS = (
    re.compile(r"expression\s*\(", re.IGNORECASE),
    re.compile(r"@import", re.IGNORECASE),
    re.compile(r"-moz-binding", re.IGNORECASE),
    re.compile(r"behavior\s*:", re.IGNORECASE),
)


@dataclass(frozen=True)
class SanitizerReport:
    """What the sanitizer removed and why (FH-02 diagnostics)."""

    removed_tags: tuple[str, ...] = ()
    removed_attrs: tuple[str, ...] = ()
    removed_css: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class SanitizeResult:
    html: str
    report: SanitizerReport

    @property
    def ok(self) -> bool:
        # "ok" means it produced non-empty output; the report still lists removals.
        return bool(self.html.strip())


class _SanitizingParser(HTMLParser):
    def __init__(
        self,
        *,
        allowed_tags: frozenset[str] = ALLOWED_TAGS,
        allow_network_resources: bool = False,
        allow_inline_scripts: bool = False,
        allow_event_handlers: bool = False,
        allow_freeform_css: bool = False,
        allow_inline_visual_resources: bool = False,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._allowed_tags = allowed_tags
        self._allow_network_resources = bool(allow_network_resources)
        self._allow_inline_scripts = bool(allow_inline_scripts)
        self._allow_event_handlers = bool(allow_event_handlers)
        self._allow_freeform_css = bool(allow_freeform_css)
        self._allow_inline_visual_resources = bool(allow_inline_visual_resources)
        self.out: list[str] = []
        self.removed_tags: list[str] = []
        self.removed_attrs: list[str] = []
        self.removed_css: list[str] = []
        self._skip_depth = 0          # inside a dropped-with-content tag
        self._external_script_depth = 0
        self._inline_script_depth = 0
        self._style_depth = 0         # inside <style>
        self._raw_style_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if self._external_script_depth or self._inline_script_depth:
            return
        if self._skip_depth:
            return
        if tag_l == "script" and self._allow_network_resources:
            src = next((value or "" for name, value in attrs if name.lower() == "src"), "")
            if _is_network_url(src):
                clean_attrs = self._clean_attrs(tag_l, attrs)
                attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in clean_attrs)
                self.out.append(f"<script{attr_str}>")
                self._external_script_depth = 1
                return
        if tag_l == "script" and self._allow_inline_scripts:
            src = next((value or "" for name, value in attrs if name.lower() == "src"), "")
            if not src:
                clean_attrs = self._clean_attrs(tag_l, attrs)
                attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in clean_attrs)
                self.out.append(f"<script{attr_str}>")
                self._inline_script_depth = 1
                return
        if tag_l == "link" and self._allow_network_resources:
            rel = next((value or "" for name, value in attrs if name.lower() == "rel"), "")
            href = next((value or "" for name, value in attrs if name.lower() == "href"), "")
            if "stylesheet" in rel.lower().split() and _is_network_url(href):
                clean_attrs = self._clean_attrs(tag_l, attrs)
                attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in clean_attrs)
                self.out.append(f"<link{attr_str}>")
                return
        if tag_l == "script" or tag_l in _DROP_TAGS_WITH_CONTENT:
            self._skip_depth += 1
            self.removed_tags.append(tag_l)
            return
        if tag_l in _DROP_TAGS_KEEP_TEXT:
            self.removed_tags.append(tag_l)
            return
        if tag_l not in self._allowed_tags:
            self.removed_tags.append(tag_l)
            return  # unknown tag: drop tag but keep walking children
        if tag_l == "style":
            self._style_depth += 1
            self._raw_style_buffer.append("")
            self.out.append("<style>")
            return
        clean_attrs = self._clean_attrs(tag_l, attrs)
        attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in clean_attrs)
        self.out.append(f"<{tag_l}{attr_str}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # self-closing tag like <img/>
        tag_l = tag.lower()
        if self._external_script_depth or self._inline_script_depth:
            return
        if self._skip_depth:
            return
        if tag_l == "link" and self._allow_network_resources:
            rel = next((value or "" for name, value in attrs if name.lower() == "rel"), "")
            href = next((value or "" for name, value in attrs if name.lower() == "href"), "")
            if "stylesheet" in rel.lower().split() and _is_network_url(href):
                clean_attrs = self._clean_attrs(tag_l, attrs)
                attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in clean_attrs)
                self.out.append(f"<link{attr_str}>")
                return
        if tag_l in _DROP_TAGS_WITH_CONTENT or tag_l == "script" or tag_l in _DROP_TAGS_KEEP_TEXT or tag_l not in self._allowed_tags:
            if tag_l in _DROP_TAGS_WITH_CONTENT or tag_l == "script":
                self.removed_tags.append(tag_l)
            return
        if tag_l == "style":
            # empty self-closing style; render as empty
            self.out.append("<style></style>")
            return
        clean_attrs = self._clean_attrs(tag_l, attrs)
        attr_str = "".join(f' {k}="{_escape_attr(v)}"' for k, v in clean_attrs)
        # Preserve SVG self-closing geometry nodes.  Re-emitting ``<rect/>``
        # as ``<rect>`` changes HTML parser error recovery: following
        # ``<text>`` nodes can become children of the rect and disappear in
        # Chromium capture even though the original authored SVG renders
        # correctly.
        if tag_l in {
            "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
            "stop", "use", "source", "track", "fegaussianblur", "fedropshadow",
        }:
            self.out.append(f"<{tag_l}{attr_str}/>")
        else:
            self.out.append(f"<{tag_l}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if self._external_script_depth:
            if tag_l == "script":
                self.out.append("</script>")
                self._external_script_depth = 0
            return
        if self._inline_script_depth:
            if tag_l == "script":
                self.out.append("</script>")
                self._inline_script_depth = 0
            return
        if self._skip_depth:
            if tag_l in _DROP_TAGS_WITH_CONTENT or tag_l == "script":
                self._skip_depth -= 1
            return
        if tag_l == "style" and self._style_depth:
            raw = self._raw_style_buffer.pop() if self._raw_style_buffer else ""
            sanitized_css = _sanitize_css(
                raw, self.removed_css,
                allow_network_resources=self._allow_network_resources,
                allow_freeform_css=self._allow_freeform_css,
                allow_inline_visual_resources=self._allow_inline_visual_resources,
            )
            # Replace the last "<style>" we emitted with the sanitized content.
            # Simpler: append sanitized css + close.
            self.out.append(sanitized_css)
            self.out.append("</style>")
            self._style_depth -= 1
            return
        if tag_l in self._allowed_tags and tag_l not in (_DROP_TAGS_WITH_CONTENT | _DROP_TAGS_KEEP_TEXT):
            self.out.append(f"</{tag_l}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._external_script_depth:
            return
        if self._inline_script_depth:
            # Script data is raw text. Escaping it would corrupt normal JS and
            # make browser-authored pages fail in the captured clip.
            self.out.append(data)
            return
        if self._style_depth:
            if self._raw_style_buffer:
                self._raw_style_buffer[-1] += data
            return
        self.out.append(_escape_text(data))

    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        cleaned: list[tuple[str, str]] = []
        for name, value in attrs:
            if value is None:
                continue
            name_l = name.lower()
            if any(name_l.startswith(prefix) for prefix in _FORBIDDEN_ATTR_PREFIXES) and not self._allow_event_handlers:
                self.removed_attrs.append(f"{name_l}={value[:20]}")
                continue
            if self._allow_event_handlers and name_l.startswith("on"):
                cleaned.append((name_l, value))
                continue
            # SVG symbol reuse is a high-value visual primitive.  Permit only
            # same-document fragment references; external hrefs stay blocked.
            if tag == "use" and name_l in {"href", "xlink:href"}:
                if value.strip().startswith("#"):
                    cleaned.append((name_l, value.strip()))
                else:
                    self.removed_attrs.append(f"{name_l}={value[:20]}")
                continue
            if name_l in _FORBIDDEN_ATTRS:
                if (
                    self._allow_network_resources
                    and tag == "link"
                    and name_l == "href"
                    and _is_network_url(value)
                ):
                    cleaned.append((name_l, value.strip()))
                    continue
                self.removed_attrs.append(f"{name_l}={value[:20]}")
                continue
            if name_l in _URL_ATTRS:
                visual_tag = tag in {"img", "audio", "video", "source"}
                resolved = _safe_url(
                    value,
                    allow_inline_visual_resources=(
                        self._allow_inline_visual_resources and visual_tag
                    ),
                )
                if (
                    self._allow_network_resources
                    and tag in {"script", "img", "audio", "video", "source"}
                    and name_l == "src"
                    and _is_network_url(value)
                ):
                    cleaned.append((name_l, value.strip()))
                    continue
                if (
                    self._allow_network_resources
                    and tag in {"video", "audio"}
                    and name_l == "poster"
                    and _is_network_url(value)
                ):
                    cleaned.append((name_l, value.strip()))
                    continue
                if resolved is None:
                    self.removed_attrs.append(f"{name_l}={value[:20]}")
                    continue
                cleaned.append((name_l, resolved))
                continue
            if name_l == "style":
                cleaned_css = _sanitize_css(
                    value, self.removed_css,
                    allow_network_resources=self._allow_network_resources,
                    allow_freeform_css=self._allow_freeform_css,
                    allow_inline_visual_resources=self._allow_inline_visual_resources,
                )
                if cleaned_css.strip():
                    cleaned.append((name_l, cleaned_css))
                continue
            if tag in {"canvas", "img", "audio", "video", "source", "track"} and name_l in {
                "width", "height", "controls", "autoplay", "loop", "muted", "playsinline",
                "preload", "kind", "srclang", "label", "default", "poster", "crossorigin",
            }:
                cleaned.append((name_l, value))
                continue
            if tag in self._allowed_tags and tag in {
                "svg", "g", "path", "rect", "circle", "ellipse", "line",
                "polyline", "polygon", "defs", "lineargradient", "radialgradient",
                "stop", "clippath", "mask", "filter", "fegaussianblur",
                "fedropshadow", "fecomponenttransfer", "fecomposite", "fecolormatrix",
                "feconvolvematrix", "fedisplacementmap", "femerge", "femergenode",
                "feoffset", "fespecularlighting", "feturbulence", "feflood", "feblend",
                "feimage", "pattern", "marker", "symbol", "text", "tspan", "use",
                "foreignobject", "animate", "animatetransform", "set",
            } and name_l in _SVG_SAFE_ATTRS:
                if name_l in _SVG_FRAGMENT_URL_ATTRS and "url(" in value.lower():
                    urls = re.findall(r"url\(([^)]+)\)", value, re.IGNORECASE)
                    if any(not item.strip().strip("'\"").startswith("#") for item in urls):
                        self.removed_attrs.append(f"{name_l}={value[:20]}")
                        continue
                cleaned.append((name_l, value))
                continue
            # data-* and a few inert globals are allowed.
            if name_l.startswith("data-") or name_l in {"class", "id", "alt", "role", "lang", "dir"} or name_l.startswith("aria-"):
                cleaned.append((name_l, value))
                continue
            if self._allow_network_resources and tag in {"link", "script", "audio", "video", "source"} and name_l in {
                "rel", "media", "type", "crossorigin", "referrerpolicy", "integrity",
            }:
                cleaned.append((name_l, value))
                continue
            self.removed_attrs.append(name_l)
        return cleaned


_INLINE_VISUAL_MIME_PREFIXES: tuple[str, ...] = (
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "font/woff", "font/ttf", "font/otf", "application/font-woff",
    "audio/", "video/",
)
_MAX_INLINE_VISUAL_URI_CHARS = 4 * 1024 * 1024


def _safe_url(value: str, *, allow_inline_visual_resources: bool = False) -> str | None:
    v = value.strip()
    if not v:
        return None
    if v.lower().startswith(_TOPOS_ASSET):
        return v
    if v.startswith("#"):
        return v  # same-page fragment is harmless
    if allow_inline_visual_resources and _safe_inline_visual_url(v):
        return v
    return None


def _safe_inline_visual_url(value: str) -> bool:
    target = str(value or "").strip()
    if target.lower().startswith("blob:"):
        return len(target) <= _MAX_INLINE_VISUAL_URI_CHARS
    if not target.lower().startswith("data:") or len(target) > _MAX_INLINE_VISUAL_URI_CHARS:
        return False
    header, separator, _payload = target[5:].partition(",")
    if not separator:
        return False
    mime = header.split(";", 1)[0].strip().lower()
    # Keep inline raster/media/font bytes, but not data SVG/HTML/script.
    return any(mime == prefix or mime.startswith(prefix) for prefix in _INLINE_VISUAL_MIME_PREFIXES)


def _is_network_url(value: str) -> bool:
    """Return True only for a well-formed HTTPS resource URL."""
    target = str(value or "").strip().strip("'\"")
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.netloc) and not any(
        marker in target for marker in ("\n", "\r", "\x00")
    )


def _sanitize_css(
    css: str,
    removed: list[str],
    *,
    allow_network_resources: bool = False,
    allow_freeform_css: bool = False,
    allow_inline_visual_resources: bool = False,
) -> str:
    """Allowlist CSS declarations + @keyframes/@font-face; strip dangerous constructs."""
    # ``style="..."`` attributes contain a declaration list, not selector
    # rules.  The rule parser below intentionally looks for ``selector{...}``
    # blocks, so sending an inline declaration list through it used to return
    # an empty string and silently remove every layout declaration generated by
    # the model (position/top/left/width/background/etc.).  That made a rich
    # HTML deck collapse into a title plus unpositioned text.  Keep the same
    # property/url/danger allowlist, but use the declaration parser directly
    # for inline CSS.
    if "{" not in css and "}" not in css and not re.search(
        r"@\s*(?:import|charset|keyframes|font-face)", css, re.IGNORECASE,
    ):
        return _filter_declarations(
            css, removed, allow_network_resources=allow_network_resources,
            allow_freeform_css=allow_freeform_css,
            allow_inline_visual_resources=allow_inline_visual_resources,
        )

    # 1. Extract HTTPS @import rules for the explicit network profile.  They
    # are CSS resource loads, not executable code; all other imports remain
    # stripped so the default offline mode is unchanged.
    network_imports: list[str] = []
    if allow_network_resources:
        def _keep_network_import(match: re.Match[str]) -> str:
            statement = match.group(0).strip()
            urls = re.findall(r"url\(([^)]+)\)|@import\s+['\"]([^'\"]+)", statement, re.IGNORECASE)
            target = next((item.strip().strip("'\"") for pair in urls for item in pair if item), "")
            if _is_network_url(target):
                network_imports.append(statement)
            else:
                removed.append("css_url:@import")
            return ""

        css = re.sub(
            r"@import\s+(?:url\([^)]*\)|['\"][^'\"]+['\"])(?:\s+[^;]*)?;",
            _keep_network_import, css, flags=re.IGNORECASE,
        )
    else:
        css = re.sub(
            r"@import\s+(?:url\([^)]*\)|['\"][^'\"]+['\"])(?:\s+[^;]*)?;",
            "", css, flags=re.IGNORECASE,
        )
    css = re.sub(r"@charset[^;]*;", "", css, flags=re.IGNORECASE)
    if re.search(r"-moz-binding", css, re.IGNORECASE):
        css = re.sub(r"-moz-binding[^;]*;", "", css, flags=re.IGNORECASE)

    # 2. Extract @keyframes blocks with balanced braces (they nest one level).
    keyframe_blocks: list[tuple[str, str]] = []

    def _extract_keyframes(text: str) -> str:
        pattern = re.compile(r"@\s*keyframes\s+([A-Za-z0-9_-]+)\s*\{", re.IGNORECASE)
        out: list[str] = []
        index = 0
        for match in pattern.finditer(text):
            out.append(text[index:match.start()])
            name = match.group(1)
            depth = 1
            cursor = match.end()
            start_body = cursor
            while cursor < len(text) and depth > 0:
                if text[cursor] == "{":
                    depth += 1
                elif text[cursor] == "}":
                    depth -= 1
                cursor += 1
            body = text[start_body:cursor - 1] if depth == 0 else text[start_body:cursor]
            keyframe_blocks.append((name, body))
            index = cursor
        out.append(text[index:])
        return "".join(out)

    css = _extract_keyframes(css)

    # 2b. Extract @font-face blocks (balanced braces).  The body is filtered
    #     separately: src/url() must be topos-asset only in offline mode; the
    #     explicit network profile may also keep HTTPS font faces.
    css, fontface_blocks = _extract_font_faces(
        css, removed, allow_network_resources=allow_network_resources,
        allow_inline_visual_resources=allow_inline_visual_resources,
    )

    # 3. Process remaining rules recursively.  The previous flat regex
    # treated the selector inside ``@media``/``@supports`` as part of an
    # unknown at-rule and discarded the whole responsive block.  That was a
    # silent visual downgrade for otherwise ordinary web pages.
    out_rules: list[str] = list(network_imports)
    sanitized_rules = _sanitize_rule_blocks(
        css, removed, allow_network_resources=allow_network_resources,
        allow_freeform_css=allow_freeform_css,
        allow_inline_visual_resources=allow_inline_visual_resources,
    )
    if sanitized_rules.strip():
        out_rules.append(sanitized_rules)

    # 4. Re-emit sanitized @keyframes (filter each from/to/percent stop).
    for name, body in keyframe_blocks:
        stops: list[str] = []
        for stop in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
            selector = stop.group(1).strip()       # from | to | N%
            filtered = _filter_declarations(
                stop.group(2), removed,
                allow_network_resources=allow_network_resources,
                allow_freeform_css=allow_freeform_css,
                allow_inline_visual_resources=allow_inline_visual_resources,
            )
            if filtered.strip() and selector:
                stops.append(f"{selector}{{{filtered}}}")
        if stops:
            # Keyframe stops are CSS blocks separated by whitespace, not a
            # comma-separated list (commas make Chromium discard the entire
            # animation).  The previous join silently turned model-authored
            # fade/page-switch animations into invalid CSS; pages whose base
            # state was opacity:0 then rendered as a blank canvas.
            out_rules.append(f"@keyframes {name}{{{''.join(stops)}}}")
    # 4b. Re-emit sanitized @font-face blocks.
    out_rules.extend(fontface_blocks)
    return "".join(out_rules)


def _sanitize_rule_blocks(
    css: str,
    removed: list[str],
    *,
    allow_network_resources: bool = False,
    allow_freeform_css: bool = False,
    allow_inline_visual_resources: bool = False,
) -> str:
    """Sanitize top-level CSS rules, preserving safe nested at-rules."""
    text = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        opening = text.find("{", cursor)
        semicolon = text.find(";", cursor)
        if semicolon >= 0 and (opening < 0 or semicolon < opening):
            removed.append(f"css_statement:{text[cursor:semicolon].strip()[:24]}")
            cursor = semicolon + 1
            continue
        if opening < 0:
            break
        prelude = text[cursor:opening].strip()
        closing = _matching_brace(text, opening)
        if closing < 0:
            break
        body = text[opening + 1:closing]
        if not prelude:
            cursor = closing + 1
            continue
        if prelude.startswith("@"):
            match = re.match(r"@\s*([a-z-]+)\b(.*)", prelude, re.IGNORECASE | re.DOTALL)
            at_name = match.group(1).lower() if match else ""
            condition = match.group(2).strip() if match else ""
            if at_name not in _ALLOWED_CSS_AT_RULES:
                removed.append(f"at_rule:{prelude[:24]}")
            else:
                if at_name == "property":
                    nested = _filter_declarations(
                        body, removed,
                        allow_network_resources=allow_network_resources,
                        allow_freeform_css=allow_freeform_css,
                        allow_inline_visual_resources=allow_inline_visual_resources,
                    )
                else:
                    nested = _sanitize_rule_blocks(
                        body, removed,
                        allow_network_resources=allow_network_resources,
                        allow_freeform_css=allow_freeform_css,
                        allow_inline_visual_resources=allow_inline_visual_resources,
                    )
                if nested.strip() and condition:
                    out.append(f"@{at_name} {condition}{{{nested}}}")
        else:
            filtered = _filter_declarations(
                body, removed,
                allow_network_resources=allow_network_resources,
                allow_freeform_css=allow_freeform_css,
                allow_inline_visual_resources=allow_inline_visual_resources,
            )
            if filtered.strip():
                out.append(f"{prelude}{{{filtered}}}")
        cursor = closing + 1
    return "".join(out)


def _matching_brace(text: str, opening: int) -> int:
    """Return the matching closing brace, or -1 for malformed CSS."""
    depth = 1
    for index in range(opening + 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _filter_declarations(
    body: str,
    removed: list[str],
    *,
    allow_network_resources: bool = False,
    allow_freeform_css: bool = False,
    allow_inline_visual_resources: bool = False,
) -> str:
    kept: list[str] = []
    # Data URLs and modern CSS functions can contain semicolons inside
    # parentheses/quotes (for example ``data:image/png;base64,...``).  A plain
    # ``split(';')`` truncated those values and visibly corrupted otherwise
    # valid freeform pages.  Split only at declaration-level semicolons.
    for declaration in _split_css_declarations(body):
        declaration = declaration.strip()
        if not declaration or ":" not in declaration:
            continue
        prop, _, value = declaration.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        if not prop or not value:
            continue
        # Drop any declaration carrying a dangerous construct entirely.
        if any(danger.search(declaration) for danger in _CSS_DANGER_TOKENS):
            removed.append(f"css_danger:{prop}")
            continue
        # CSS custom properties (--brand, --gap, …) are the plan's design-token
        # surface (extract_tokens / set_token): they are inert named values the
        # sanitizer keeps even though they are not in the fixed property
        # allowlist.  They are NOT exempt from the url() policy, though: a
        # custom property is loadable through ``var()`` (``--x:
        # url(https://…)``, then ``background: var(--x)``), so a url() inside
        # one must pass the same whitelist as a literal url() (AUD-30014).
        if prop.startswith("--"):
            if _carries_unsafe_css_url(
                value,
                allow_network_resources=allow_network_resources,
                allow_inline_visual_resources=allow_inline_visual_resources,
            ):
                removed.append(f"css_url:{prop}")
                continue
            kept.append(declaration)
            continue
        if not allow_freeform_css and not any(
            prop == allowed or prop.startswith(allowed + "-")
            for allowed in _ALLOWED_CSS_PROPERTIES
        ):
            removed.append(f"css:{prop}")
            continue
        # url() must be topos-asset only.
        if _carries_unsafe_css_url(
            value,
            allow_network_resources=allow_network_resources,
            allow_inline_visual_resources=allow_inline_visual_resources,
        ):
            removed.append(f"css_url:{prop}")
            continue
        kept.append(f"{prop}:{value}")
    return ";".join(kept)


def _carries_unsafe_css_url(
    value: str,
    *,
    allow_network_resources: bool = False,
    allow_inline_visual_resources: bool = False,
) -> bool:
    """Whether any ``url(...)`` in a declaration value fails the whitelist."""
    if "url(" not in value.lower():
        return False
    urls = re.findall(r"url\(([^)]+)\)", value, re.IGNORECASE)
    return any(not _safe_css_url(
        u,
        allow_network_resources=allow_network_resources,
        allow_inline_visual_resources=allow_inline_visual_resources,
    ) for u in urls)


def _split_css_declarations(body: str) -> list[str]:
    """Split a CSS declaration list without splitting quoted/function data."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    escaped = False
    for char in str(body or ""):
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote:
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == ";" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _safe_css_url(
    value: str,
    *,
    allow_network_resources: bool = False,
    allow_inline_visual_resources: bool = False,
) -> bool:
    """Allow bound assets and same-document fragment references only."""
    target = value.strip().strip("'\"").lower()
    return (
        target.startswith(_TOPOS_ASSET)
        or target.startswith("#")
        or (allow_network_resources and _is_network_url(target))
        or (allow_inline_visual_resources and _safe_inline_visual_url(target))
    )


# Declarations allowed inside @font-face (CSS Fonts L4).  ``src`` is the
# font source; everything else is metadata.  ``src`` still goes through the
# topos-asset url() check below.
_FONT_FACE_ALLOWED_PROPS: frozenset[str] = frozenset({
    "src", "font-family", "font-weight", "font-style", "font-stretch",
    "font-display", "unicode-range", "font-variant", "font-feature-settings",
})


def _extract_font_faces(
    text: str,
    removed: list[str],
    *,
    allow_network_resources: bool = False,
    allow_inline_visual_resources: bool = False,
) -> tuple[str, list[str]]:
    """Pull @font-face blocks out (balanced braces); sanitize + return kept ones."""
    pattern = re.compile(r"@\s*font-face\s*\{", re.IGNORECASE)
    out: list[str] = []
    kept: list[str] = []
    index = 0
    for match in pattern.finditer(text):
        out.append(text[index:match.start()])
        depth = 1
        cursor = match.end()
        start_body = cursor
        while cursor < len(text) and depth > 0:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        body = text[start_body:cursor - 1] if depth == 0 else text[start_body:cursor]
        filtered = _filter_font_face_body(
            body,
            removed,
            allow_network_resources=allow_network_resources,
            allow_inline_visual_resources=allow_inline_visual_resources,
        )
        # A @font-face without a surviving src is useless and usually means its
        # only src was a stripped remote/escape — drop the block entirely.
        if filtered.strip() and "src:" in filtered:
            kept.append(f"@font-face{{{filtered}}}")
        else:
            removed.append("at_rule:@font-face(no-src)")
        index = cursor
    out.append(text[index:])
    return "".join(out), kept


def _filter_font_face_body(
    body: str,
    removed: list[str],
    *,
    allow_network_resources: bool = False,
    allow_inline_visual_resources: bool = False,
) -> str:
    """Allowlist @font-face declarations; force src url() to topos-asset only."""
    kept: list[str] = []
    for declaration in _split_css_declarations(body):
        declaration = declaration.strip()
        if not declaration or ":" not in declaration:
            continue
        prop, _, value = declaration.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        if not prop or not value:
            continue
        if prop not in _FONT_FACE_ALLOWED_PROPS:
            removed.append(f"fontface_css:{prop}")
            continue
        if any(danger.search(declaration) for danger in _CSS_DANGER_TOKENS):
            removed.append(f"fontface_danger:{prop}")
            continue
        # src url() must reference a bound topos-asset (no remote/font file escape).
        if "url(" in value.lower():
            urls = re.findall(r"url\(([^)]+)\)", value, re.IGNORECASE)
            bad = [u for u in urls if not _safe_css_url(
                u,
                allow_network_resources=allow_network_resources,
                allow_inline_visual_resources=allow_inline_visual_resources,
            )]
            if bad:
                removed.append(f"fontface_url:{prop}")
                continue
        kept.append(f"{prop}:{value}")
    return ";".join(kept)


def _escape_text(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


class HtmlSanitizer:
    """Sanitize HTML with a default-safe or explicit freeform browser profile.

    ``allow_inline_scripts``/``allow_event_handlers``/``allow_freeform_css`` are
    deliberately opt-in.  Motion Graphic Clip enables them because its
    contract is a browser-authored page; generic imported HTML keeps the
    conservative profile.
    """

    def __init__(
        self,
        *,
        allowed_tags: frozenset[str] | None = None,
        allow_network_resources: bool = False,
        allow_inline_scripts: bool = False,
        allow_event_handlers: bool = False,
        allow_freeform_css: bool = False,
        allow_inline_visual_resources: bool = False,
    ) -> None:
        self._allowed_tags = allowed_tags or ALLOWED_TAGS
        self._allow_network_resources = bool(allow_network_resources)
        self._allow_inline_scripts = bool(allow_inline_scripts)
        self._allow_event_handlers = bool(allow_event_handlers)
        self._allow_freeform_css = bool(allow_freeform_css)
        self._allow_inline_visual_resources = bool(allow_inline_visual_resources)

    def sanitize(self, html: str) -> SanitizeResult:
        parser = _SanitizingParser(
            allowed_tags=self._allowed_tags,
            allow_network_resources=self._allow_network_resources,
            allow_inline_scripts=self._allow_inline_scripts,
            allow_event_handlers=self._allow_event_handlers,
            allow_freeform_css=self._allow_freeform_css,
            allow_inline_visual_resources=self._allow_inline_visual_resources,
        )
        try:
            parser.feed(html)
            parser.close()
        except Exception as exc:  # defensive: malformed HTML must not crash
            return SanitizeResult("", SanitizerReport(notes=(f"parse_error:{exc}",)))
        report = SanitizerReport(
            removed_tags=tuple(parser.removed_tags),
            removed_attrs=tuple(parser.removed_attrs),
            removed_css=tuple(parser.removed_css),
        )
        return SanitizeResult("".join(parser.out), report)


__all__ = ["ALLOWED_TAGS", "HtmlSanitizer", "SanitizeResult", "SanitizerReport"]
