"""HTML motion-layer font lock: pin rendering to packaged OFL fonts.

确定性立身之本 (W0-A): the HTML motion layer renders with ONE pinned,
project-packaged font set (DejaVu, OFL) injected via ``@font-face`` with the
bytes inlined as base64 ``data:`` URIs, so the same HTML produces
byte-identical RGBA on any machine/OS — ``frame_hash`` no longer drifts with
system fonts (the latent cross-OS determinism bug behind the ubuntu/macos CI
gate).  The bytes are inlined (not served via ``topos-asset://``) because
Chromium will not fetch ``@font-face`` from the custom scheme, so the lock
would otherwise silently fall back to a system font.

Strategy (forced alias injection, per product decision): a trusted CSS block is
prepended to the sanitized HTML at render time. It binds the private families
``__topos_sans`` / ``__topos_mono`` to the packaged DejaVu bytes and forces
them with ``*{font-family:... !important}``. Whatever the model wrote
(Arial / Helvetica / sans-serif / …) resolves to the same DejaVu bytes, so
glyph rasterisation is a pure function of (renderer_build, source, font bytes).

The font *file hashes* feed ``font_set_hash`` → ``RenderEvidence`` (and, when
wired, the cache key), so a font-file bump invalidates frames exactly as a
renderer bump does.
"""

from __future__ import annotations

import base64
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from .font_registry import FontEntry, FontLicense, FontRegistry


# Stable topos-asset:// ids for the packaged fonts (versioned, hash-pinned).
_FONT_SANS = "font/dejavu-sans"
_FONT_SANS_BOLD = "font/dejavu-sans-bold"
_FONT_MONO = "font/dejavu-sans-mono"
DEFAULT_FONT_ASSET_IDS: tuple[str, ...] = (_FONT_SANS, _FONT_SANS_BOLD, _FONT_MONO)

# asset_id -> (source ttf, delivery woff2, family, subfamily).  The source TTF
# is hashed for font_set_hash (the determinism identity — stable regardless of
# delivery compression, so a TTF<->woff2 switch never churns caches); the woff2
# is what the font lock inlines (~65% smaller than the TTF).
_PACK: Mapping[str, tuple[str, str, str, str]] = {
    _FONT_SANS: ("DejaVuSans.ttf", "DejaVuSans.woff2", "DejaVu Sans", "Regular"),
    _FONT_SANS_BOLD: ("DejaVuSans-Bold.ttf", "DejaVuSans-Bold.woff2", "DejaVu Sans", "Bold"),
    _FONT_MONO: ("DejaVuSansMono.ttf", "DejaVuSansMono.woff2", "DejaVu Sans Mono", "Regular"),
}

# matplotlib bundles DejaVu 2.37; recorded so a font-file bump is visible in
# the FontEntry version and therefore in font_set_hash.
_FONT_VERSION = "dejavu-2.37"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_assets_dir() -> Path:
    # src/features/teaching_animation/core/html_fonts.py -> src/assets/fonts
    return Path(__file__).resolve().parents[3] / "assets" / "fonts"


def _resolve_dir(assets_dir: Path | None) -> Path:
    return Path(assets_dir) if assets_dir is not None else _default_assets_dir()


def load_default_html_font_pack(assets_dir: Path | None = None) -> tuple[FontEntry, ...]:
    """Read the packaged fonts and build FontEntry with real file hashes."""
    base = _resolve_dir(assets_dir)
    entries: list[FontEntry] = []
    for asset_id, (ttf, _woff2, family, subfamily) in _PACK.items():
        data = (base / ttf).read_bytes()
        entries.append(FontEntry(
            family=family,
            subfamily=subfamily,
            version=_FONT_VERSION,
            file_hash=_sha256_bytes(data),
            file_uri=f"src/assets/fonts/{ttf}",   # project-relative, portable
            license=FontLicense.OPEN,
            formats=("ttf", "woff2"),
        ))
    return tuple(entries)


def default_html_font_registry(assets_dir: Path | None = None) -> FontRegistry:
    """A FontRegistry pre-loaded with the default HTML font pack."""
    registry = FontRegistry()
    for entry in load_default_html_font_pack(assets_dir):
        registry.register(entry)
    return registry


def default_font_asset_bytes(assets_dir: Path | None = None) -> dict[str, bytes]:
    """asset_id -> delivery (woff2) font bytes the lock inlines as data: URIs."""
    base = _resolve_dir(assets_dir)
    return {asset_id: (base / woff2).read_bytes()
            for asset_id, (_ttf, woff2, _f, _s) in _PACK.items()}


def default_font_set_hash(assets_dir: Path | None = None) -> str:
    """Aggregate font file hashes -> one hash (RenderEvidence.font_set_hash / cache key)."""
    entries = load_default_html_font_pack(assets_dir)
    by_id = dict(zip(DEFAULT_FONT_ASSET_IDS, entries))
    parts = sorted(f"{aid}:{by_id[aid].file_hash}" for aid in DEFAULT_FONT_ASSET_IDS)
    return _sha256_bytes("|".join(parts).encode("utf-8"))


@lru_cache(maxsize=8)
def _data_uri_font_lock_css(assets_dir_key: str) -> str:
    """Build the font-lock CSS with fonts embedded as base64 ``data:`` URIs.

    Chromium will not fetch ``@font-face`` from the custom ``topos-asset://``
    scheme (it only fetches subresources from known schemes), so the packaged
    font bytes must be inlined as ``data:`` URIs for the lock to actually take
    effect — otherwise ``__topos_sans`` silently falls back to a system font and
    cross-machine determinism is lost (the latent bug behind the lock).  The
    bytes are the woff2 delivery form (~65% smaller than TTF).  Cached: the
    packaged font set is immutable, so the CSS is built once per assets dir, not
    per frame.
    """
    assets_dir = Path(assets_dir_key) if assets_dir_key else None
    fb = default_font_asset_bytes(assets_dir)

    def _face(family: str, asset_id: str, weight: str = "") -> str:
        b64 = base64.b64encode(fb[asset_id]).decode("ascii")
        weight_decl = f";font-weight:{weight}" if weight else ""
        return (f"@font-face{{font-family:'{family}';"
                f"src:url(data:font/woff2;base64,{b64}) format('woff2'){weight_decl}}}")

    return (
        _face("__topos_sans", _FONT_SANS)
        + _face("__topos_sans", _FONT_SANS_BOLD, "bold")
        + _face("__topos_mono", _FONT_MONO)
        + "*{font-family:'__topos_sans',sans-serif!important}"
        + "code,pre,kbd,samp,tt{font-family:'__topos_mono',monospace!important}"
    )


def default_font_lock_css(assets_dir: Path | None = None) -> str:
    """The forced-alias CSS binding generic families to packaged DejaVu bytes.

    Injected as a trusted ``<style>`` (it never passes through the sanitizer; it
    is generated by the host, not the model). The fonts are embedded as base64
    woff2 ``data:`` URIs so the lock is self-contained and actually loads under
    Chromium (which won't fetch ``@font-face`` from ``topos-asset://``). The
    ``!important`` guarantees the model's own ``font-family`` declarations
    resolve to these bytes regardless of what family names it used.
    """
    return _data_uri_font_lock_css("" if assets_dir is None else str(assets_dir))


def wrap_with_font_lock(sanitized_html: str, css: str | None = None) -> str:
    """Prepend the font-lock CSS as a ``<style>`` inside the document ``<head>``.

    ``css`` may carry a precomputed lock (e.g. a renderer session's
    ``font_lock_css``) so the lock is built once per scene rather than
    recomputed per frame.  Renders use this wrapped HTML so the bytes captured
    are font-locked; the layer's stored ``sanitized_html`` is untouched (the
    lock is a render-time concern, not authored content).
    """
    lock_css = css if css is not None else default_font_lock_css()
    lock = f"<style id='topos-font-lock'>{lock_css}</style>"
    head = re.search(r"<head[^>]*>", sanitized_html, re.IGNORECASE)
    if head:
        return sanitized_html[:head.end()] + lock + sanitized_html[head.end():]
    html_tag = re.search(r"<html[^>]*>", sanitized_html, re.IGNORECASE)
    if html_tag:
        return sanitized_html[:html_tag.end()] + lock + sanitized_html[html_tag.end():]
    return lock + sanitized_html


__all__ = [
    "DEFAULT_FONT_ASSET_IDS",
    "default_font_asset_bytes",
    "default_font_lock_css",
    "default_font_set_hash",
    "default_html_font_registry",
    "load_default_html_font_pack",
    "wrap_with_font_lock",
]
