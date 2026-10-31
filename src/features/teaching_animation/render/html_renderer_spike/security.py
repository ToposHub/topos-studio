"""W0-A spike: the runtime sandbox profile applied to every HTML capture.

The plan (FH-12 / W0-A) requires defence in depth: a sanitizer alone is not
enough, because CSS can still escape via resources, navigation, popups,
downloads, storage or browser APIs.  This module defines the *runtime* layer
that the capture backend applies around the page:

* a strict Content-Security-Policy;
* a deny-by-default URL request policy with a single allow-listed scheme;
* the read-only ``topos-asset://`` scheme handler contract;
* explicit resource/complexity budgets.

Everything here is pure data and pure functions so it can be unit-tested without
a real browser.  The capture backend is responsible for enforcing the policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import urllib.parse
from typing import Mapping


# The only URL schemes a captured HTML page may resolve by default.  The
# freeform Motion Graphic browser profile can additionally opt into HTTPS
# resources and bounded *visual* data/blob URLs.  Local/file/javascript URLs
# remain unavailable in every profile.  A visual data URL is deliberately not
# a general script escape: its MIME type and request kind are checked below.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"topos-asset"})
# ``about:blank`` is permitted only as the page origin (setHtml base), never as
# a fetch/navigation target from authored content.
PAGE_ORIGIN = "about:blank"

# Capabilities disabled by the conservative generic profile.  The explicit
# Motion Graphic freeform profile re-enables authored scripts/network while
# keeping the host boundary (files, popups, downloads, storage and permissions).
DISABLED_FEATURES: tuple[str, ...] = (
    "scripts",            # disabled by default; freeform may opt in
    "network",            # disabled by default; freeform may opt into HTTPS
    "file-system",        # no file: scheme, no local file access
    "downloads",          # no triggered downloads
    "popups",             # no window.open / target=_blank
    "devtools",           # no remote debugging
    "permissions",        # no geolocation/camera/mic/notifications
    "storage",            # no cookies / localStorage / IndexedDB persistence
    "gpu-apis",           # conservative profile; freeform Motion Graphic may opt in
)


class ResourceDecision(str, Enum):
    """Verdict returned by the URL policy for a single resource request."""

    ALLOW = "allow"            # topos-asset://<id> resolving to a bound asset
    DENY = "deny"              # everything else, with a coded reason


@dataclass(frozen=True)
class UrlRequest:
    """A single resource/navigation request intercepted by the runtime."""

    url: str
    kind: str = "resource"     # resource | navigation | popup | download
    method: str = "GET"


@dataclass(frozen=True)
class UrlPolicyDecision:
    decision: ResourceDecision
    reason: str
    request: UrlRequest


class UrlPolicy:
    """URL policy with an explicit optional HTTPS browser-resource profile.

    ``topos-asset://<asset_id>`` remains the default.  Motion Graphic Clip may
    opt into ``allow_network_resources`` so authored websites can load public
    HTTPS CSS, fonts, images and CDN scripts.  A separate
    ``allow_inline_visual_resources`` opt-in permits bounded raster/font/media
    data and blob URLs, but still denies executable data, file, javascript and
    mailto schemes.  No profile exposes a project filesystem or host bridge.
    When ``allowed_network_hosts`` is non-empty, only those HTTPS hosts are
    reachable.
    """

    def __init__(
        self,
        allowed_asset_ids: frozenset[str] | None = None,
        *,
        allow_network_resources: bool = False,
        allowed_network_hosts: frozenset[str] | None = None,
        allow_inline_visual_resources: bool = False,
        allow_gpu_apis: bool = False,
    ) -> None:
        self._allowed = frozenset(allowed_asset_ids or ())
        self._allow_network_resources = bool(allow_network_resources)
        self._allowed_network_hosts = frozenset(
            str(host).strip().lower().rstrip(".")
            for host in (allowed_network_hosts or ())
            if str(host).strip()
        )
        self._allow_inline_visual_resources = bool(allow_inline_visual_resources)
        self._allow_gpu_apis = bool(allow_gpu_apis)

    @property
    def allowed_asset_ids(self) -> frozenset[str]:
        return self._allowed

    @property
    def allow_network_resources(self) -> bool:
        return self._allow_network_resources

    @property
    def allowed_network_hosts(self) -> frozenset[str]:
        return self._allowed_network_hosts

    @property
    def allow_inline_visual_resources(self) -> bool:
        return self._allow_inline_visual_resources

    @property
    def allow_gpu_apis(self) -> bool:
        return self._allow_gpu_apis

    def evaluate(self, request: UrlRequest) -> UrlPolicyDecision:
        url = request.url.strip()
        if " " in url or "\t" in url:
            return UrlPolicyDecision(ResourceDecision.DENY, "url_contains_whitespace", request)
        # AUD-30218: WHATWG URL treats ``\`` as ``/`` inside special schemes
        # and browsers strip ASCII control characters before parsing, so a
        # hand-rolled parser can be talked into approving a different host
        # than the browser will actually contact (e.g.
        # ``https://evil.example\@allowed.com/x``).  Deny the ambiguity
        # outright instead of trying to normalize it.
        if "\\" in url or any(_is_ascii_control(ch) for ch in url):
            return UrlPolicyDecision(
                ResourceDecision.DENY, "url_contains_backslash_or_control", request)
        scheme, rest = _split_scheme(url)
        if scheme is None:
            return UrlPolicyDecision(ResourceDecision.DENY, "relative_or_missing_scheme", request)
        if scheme == "topos-asset":
            # Asset ids are namespaced paths (for example
            # ``font/dejavu-sans`` or ``image/logo``); only strip query and
            # fragment suffixes, never the first path segment.
            asset_id = rest.split("?", 1)[0].split("#", 1)[0]
            if not asset_id:
                return UrlPolicyDecision(ResourceDecision.DENY, "asset_id_empty", request)
            # Deny-by-default: an asset id must be explicitly bound for this
            # capture, otherwise the request is rejected even when the policy
            # has no bindings at all.
            if asset_id not in self._allowed and asset_id.split("/", 1)[0] not in self._allowed:
                return UrlPolicyDecision(ResourceDecision.DENY, "asset_id_not_bound", request)
            return UrlPolicyDecision(ResourceDecision.ALLOW, "bound_asset", request)
        if scheme in {"http", "https", "ws", "wss"}:
            if scheme != "https" or not self._allow_network_resources:
                return UrlPolicyDecision(ResourceDecision.DENY, "network_scheme_blocked", request)
            # Network resources are intentionally HTTPS-only.  The host is
            # parsed with the standard authority grammar (userinfo stripped,
            # port stripped, IPv6 brackets removed) so ``user@``-style
            # obfuscation cannot smuggle a host past the allow-list.
            host = _network_host(url)
            if host is None:
                return UrlPolicyDecision(ResourceDecision.DENY, "network_host_missing", request)
            if self._allowed_network_hosts and host not in self._allowed_network_hosts:
                return UrlPolicyDecision(ResourceDecision.DENY, "network_host_not_allowed", request)
            return UrlPolicyDecision(ResourceDecision.ALLOW, "https_network_resource", request)
        if scheme == "file":
            return UrlPolicyDecision(ResourceDecision.DENY, "file_scheme_blocked", request)
        if scheme == "data":
            if self._allow_inline_visual_resources and _safe_visual_data_uri(url, request.kind):
                return UrlPolicyDecision(ResourceDecision.ALLOW, "inline_visual_data_uri", request)
            return UrlPolicyDecision(ResourceDecision.DENY, "data_uri_blocked", request)
        if scheme == "blob":
            # Blob URLs are useful for a canvas-generated image/video/font, but
            # must never become a script/worker/navigation escape hatch.
            if self._allow_inline_visual_resources and request.kind in _VISUAL_REQUEST_KINDS:
                return UrlPolicyDecision(ResourceDecision.ALLOW, "inline_visual_blob_uri", request)
            return UrlPolicyDecision(ResourceDecision.DENY, "blob_uri_blocked", request)
        if scheme == "javascript":
            return UrlPolicyDecision(ResourceDecision.DENY, "javascript_uri_blocked", request)
        if scheme == "mailto":
            return UrlPolicyDecision(ResourceDecision.DENY, "mailto_blocked", request)
        return UrlPolicyDecision(ResourceDecision.DENY, "unrecognized_scheme", request)


def _split_scheme(url: str) -> tuple[str | None, str]:
    """Split a URL into (scheme_lower, rest) without importing urllib.

    Handles both hierarchical (``scheme://...``) and opaque (``scheme:...``)
    forms so that ``data:``, ``javascript:``, ``blob:`` and ``mailto:`` are
    classified by scheme.  Returns (None, url) when there is no valid leading
    scheme.  A leading scheme must match ``[a-z][a-z0-9+.-]*:`` (case-insensitive).
    """
    colon = url.find(":")
    if colon <= 0:
        return None, url
    scheme = url[:colon]
    if not scheme or not scheme[0].isalpha():
        return None, url
    if not all(ch.isalnum() or ch in "+.-" for ch in scheme):
        return None, url
    rest = url[colon + 1:]
    if rest.startswith("//"):
        rest = rest[2:]
    return scheme.lower(), rest


def _is_ascii_control(ch: str) -> bool:
    """True for C0 controls and DEL — stripped by browsers before URL parsing."""
    return ord(ch) < 0x20 or ord(ch) == 0x7F


def _network_host(url: str) -> str | None:
    """Extract the lower-cased network host with the standard grammar.

    ``urllib.parse.urlsplit().hostname`` applies the spec's authority rules
    (userinfo stripped at the last ``@``, port removed, IPv6 brackets
    dropped, host lower-cased), which tracks browser behaviour far closer
    than an ad-hoc ``split`` chain.  Returns ``None`` for a missing or
    unparseable host (e.g. malformed IPv6 brackets), which callers treat as
    a denial (AUD-30218).
    """
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.strip().lower().rstrip(".")


_VISUAL_REQUEST_KINDS: frozenset[str] = frozenset({"image", "font", "media", "resource"})
_INLINE_VISUAL_MIME_PREFIXES: tuple[str, ...] = (
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "font/woff", "font/ttf", "font/otf", "application/font-woff",
    "audio/", "video/",
)
_MAX_INLINE_VISUAL_URI_CHARS = 4 * 1024 * 1024


def _safe_visual_data_uri(url: str, request_kind: str) -> bool:
    """Allow bounded raster/font/media data only for visual requests.

    In particular, ``data:text/html`` and ``data:application/javascript`` are
    never accepted.  SVG data is also excluded because inline SVG already has
    a first-class sanitized representation and an SVG data document can carry
    a second parser/execution surface.
    """
    if request_kind not in _VISUAL_REQUEST_KINDS or len(url) > _MAX_INLINE_VISUAL_URI_CHARS:
        return False
    payload = url[5:].split(",", 1)
    if len(payload) != 2:
        return False
    mime = payload[0].split(";", 1)[0].strip().lower()
    return any(mime == prefix or mime.startswith(prefix) for prefix in _INLINE_VISUAL_MIME_PREFIXES)


@dataclass(frozen=True)
class CspPolicy:
    """Strict Content-Security-Policy for captured HTML pages.

    ``default-src 'none'`` denies everything by default; each permitted source
    is added explicitly.  Inline styles are allowed (CSS animation is in scope).
    The optional network profile adds HTTPS to styles/fonts/images/scripts and
    connect sources, while frame/worker/object/navigation capabilities remain
    disabled.
    """

    allowed_asset_ids: frozenset[str] = field(default_factory=frozenset)
    allow_network_resources: bool = False
    allowed_network_hosts: frozenset[str] = field(default_factory=frozenset)
    allow_inline_scripts: bool = False
    allow_inline_visual_resources: bool = False

    def to_header(self) -> str:
        asset_source = " ".join(f"topos-asset://{aid}" for aid in sorted(self.allowed_asset_ids))
        network_sources: list[str] = []
        if self.allow_network_resources:
            # A host allow-list can be represented in CSP as exact HTTPS
            # origins.  An empty list intentionally means any HTTPS CDN; the
            # request interceptor remains the second enforcement layer.
            network_sources = (
                [f"https://{host}" for host in sorted(self.allowed_network_hosts)]
                if self.allowed_network_hosts else ["https:"]
            )
        def _src(name: str, sources: list[str]) -> str:
            return name + ((" " + " ".join(sources)) if sources else "")

        image_sources = ([asset_source] if asset_source else []) + network_sources
        if self.allow_network_resources:
            image_sources.append("data:")
        if self.allow_inline_visual_resources:
            image_sources.extend(("data:", "blob:"))
        font_sources = list(image_sources)
        media_sources = list(network_sources)
        if self.allow_inline_visual_resources:
            media_sources.extend(("data:", "blob:"))
        script_sources = list(network_sources)
        if self.allow_inline_scripts:
            # This is only enabled for the explicit Motion Graphic freeform
            # browser profile. The page still has no file/host bridge, popup,
            # download, frame or persistent-storage capability.
            script_sources.insert(0, "'unsafe-inline'")
        parts = [
            "default-src 'none'",
            _src("style-src 'unsafe-inline'", network_sources),
            _src("img-src", image_sources),
            _src("font-src", font_sources),
            "media-src " + (" ".join(media_sources) if media_sources else "'none'"),
            "script-src " + (" ".join(script_sources) if script_sources else "'none'"),
            "connect-src " + (" ".join(network_sources) if self.allow_network_resources else "'none'"),
            "frame-src 'none'",
            "child-src 'none'",
            "worker-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
            "navigate-to 'none'",
        ]
        return "; ".join(part for part in parts if part)


def ensure_font_lock_csp(csp: str) -> str:
    """Guarantee a CSP lets the font-lock ``@font-face`` load its embedded fonts.

    The font lock inlines the packaged fonts as base64 ``data:`` URIs (Chromium
    will not fetch ``@font-face`` from the custom ``topos-asset://`` scheme), so
    the CSP's ``font-src`` must allow ``data:``.  A layer CSP without it would
    block the ``@font-face`` and fall back to a system font — silently breaking
    the cross-machine determinism lock the font lock exists to enforce.  Merges
    ``data:`` into any existing ``font-src`` or adds the directive; returns the
    CSP unchanged when ``data:`` is already allowed.
    """
    directives = [d.strip() for d in csp.split(";") if d.strip()]
    out: list[str] = []
    handled = False
    for directive in directives:
        parts = directive.split(None, 1)
        name = parts[0]
        sources = parts[1].split() if len(parts) > 1 else []
        if name.lower() == "font-src":
            if "data:" not in sources:
                sources = ["data:"] + sources
            out.append("font-src " + " ".join(sources))
            handled = True
        else:
            out.append(directive)
    if not handled:
        out.append("font-src data:")
    return "; ".join(out)


@dataclass(frozen=True)
class ResourceBudget:
    """Hard limits that prevent runaway DOM/CSS from freezing the renderer.

    Exceeding any limit must produce a structured CSS-complexity failure rather
    than an indefinite render.  These are conservative first values; the ADR
    will pin measured numbers after the W0-A baseline.
    """

    max_dom_nodes: int = 4000
    # This is a runaway-DOM safety guard, not a design limit.  Rich HTML/CSS
    # compositions can legitimately nest beyond the old 48-level value.
    max_dom_depth: int = 96
    max_stylesheet_chars: int = 200_000
    max_keyframes: int = 128
    max_filter_layers: int = 8
    max_shadow_layers: int = 6
    max_render_ms_soft: int = 4000        # soft target before timeout
    max_data_uri_bytes: int = 32_000      # individual inline data: payload

    def to_dict(self) -> dict[str, int]:
        return {
            "max_dom_nodes": self.max_dom_nodes,
            "max_dom_depth": self.max_dom_depth,
            "max_stylesheet_chars": self.max_stylesheet_chars,
            "max_keyframes": self.max_keyframes,
            "max_filter_layers": self.max_filter_layers,
            "max_shadow_layers": self.max_shadow_layers,
            "max_render_ms_soft": self.max_render_ms_soft,
            "max_data_uri_bytes": self.max_data_uri_bytes,
        }


@dataclass(frozen=True)
class BudgetViolation:
    budget_field: str
    actual: int
    limit: int


def check_stylesheet_budget(css: str, budget: ResourceBudget) -> tuple[BudgetViolation | None, int]:
    """Return the first budget violation in a stylesheet plus its keyframe count."""
    keyframes = css.lower().count("@keyframes")
    if len(css) > budget.max_stylesheet_chars:
        return BudgetViolation("max_stylesheet_chars", len(css), budget.max_stylesheet_chars), keyframes
    if keyframes > budget.max_keyframes:
        return BudgetViolation("max_keyframes", keyframes, budget.max_keyframes), keyframes
    return None, keyframes


@dataclass(frozen=True)
class ToposAssetBinding:
    """A resolved, read-only binding for the ``topos-asset://`` scheme.

    The capture backend resolves ``topos-asset://<asset_id>`` to a local
    read-only file via this binding.  HTML never sees an absolute path — only
    the opaque asset id.  Paths must be relative (resolved by the host) to keep
    ``.topos`` projects portable, mirroring ``SceneAssetRef``.
    """

    asset_id: str
    resolved_path: str       # absolute, host-resolved; never authored
    mime_type: str
    sha256: str = ""

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not self.resolved_path:
            raise ValueError("resolved_path cannot be empty")
        if not self.mime_type:
            raise ValueError("mime_type cannot be empty")


@dataclass(frozen=True)
class ToposAssetRegistry:
    """Maps bound asset ids to read-only resolutions for one capture."""

    bindings: Mapping[str, ToposAssetBinding] = field(default_factory=dict)

    def resolve(self, asset_id: str) -> ToposAssetBinding | None:
        return self.bindings.get(asset_id)

    def allowed_ids(self) -> frozenset[str]:
        return frozenset(self.bindings.keys())


__all__ = [
    "ALLOWED_SCHEMES",
    "BudgetViolation",
    "CspPolicy",
    "DISABLED_FEATURES",
    "PAGE_ORIGIN",
    "ResourceBudget",
    "ResourceDecision",
    "ToposAssetBinding",
    "ToposAssetRegistry",
    "UrlPolicy",
    "UrlPolicyDecision",
    "UrlRequest",
    "check_stylesheet_budget",
    "ensure_font_lock_csp",
]
