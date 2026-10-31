"""RD-06: font registry — pinned families, fallback chains, glyph coverage.

Font substitution silently changes wrapping and metrics; this module makes it a
diagnosable decision.  A ``FontEntry`` pins family/subfamily/version/file hash/
license + glyph coverage; the ``FontRegistry`` resolves a family to an entry and
reports missing codepoints for a given string across a fallback chain.  No-licence
or missing-glyph fonts are reported so they can block/warn before delivery.

Coverage is expressed as a coarse-grained set of Unicode "blocks" (Basic Latin,
CJK Unified, etc.) plus an optional explicit codepoint set — enough to decide
fallback deterministically without bundling a full shaping engine here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping


class FontLicense(str, Enum):
    OPEN = "open"             # OFL/Apache/MIT — embed + deliver
    EMBEDDED = "embedded"     # licensed for embedding in this project only
    PREVIEW_ONLY = "preview_only"
    MISSING = "missing"


# Coarse Unicode-block tags a font may declare coverage for.
GLYPH_BLOCKS = (
    "basic_latin", "latin_extended", "cyrillic", "greek", "arabic", "hebrew",
    "cjk_unified", "cjk_ext_a", "hangul", "hiragana", "katakana", "emoji",
)


def _block_for_codepoint(codepoint: int) -> str | None:
    if 0x0000 <= codepoint <= 0x007F:
        return "basic_latin"
    if 0x0080 <= codepoint <= 0x024F:
        return "latin_extended"
    if 0x0400 <= codepoint <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= codepoint <= 0x03FF:
        return "greek"
    if 0x0590 <= codepoint <= 0x05FF:
        return "hebrew"
    if 0x0600 <= codepoint <= 0x06FF:
        return "arabic"
    if 0x3040 <= codepoint <= 0x309F:
        return "hiragana"
    if 0x30A0 <= codepoint <= 0x30FF:
        return "katakana"
    if 0xAC00 <= codepoint <= 0xD7AF:
        return "hangul"
    if 0x4E00 <= codepoint <= 0x9FFF:
        return "cjk_unified"
    if 0x3400 <= codepoint <= 0x4DBF:
        return "cjk_ext_a"
    if 0x1F300 <= codepoint <= 0x1FAFF:
        return "emoji"
    return None


@dataclass(frozen=True)
class FontEntry:
    """A pinned, auditable font file."""

    family: str
    subfamily: str = "Regular"
    version: str = "1.0"
    file_hash: str = ""
    file_uri: str = ""              # project-relative
    license: FontLicense = FontLicense.MISSING
    formats: tuple[str, ...] = ("ttf",)
    coverage_blocks: frozenset[str] = field(default_factory=frozenset)
    explicit_codepoints: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.family:
            raise ValueError("family cannot be empty")
        if self.file_uri.startswith("/"):
            raise ValueError("font file_uri must be relative, not absolute")
        unknown = self.coverage_blocks - frozenset(GLYPH_BLOCKS)
        if unknown:
            raise ValueError(f"unknown glyph blocks: {sorted(unknown)}")

    @property
    def key(self) -> str:
        return f"{self.family}/{self.subfamily}"

    def covers_codepoint(self, codepoint: int) -> bool:
        if codepoint in self.explicit_codepoints:
            return True
        block = _block_for_codepoint(codepoint)
        return block is not None and block in self.coverage_blocks

    def covers_text(self, text: str) -> tuple[bool, tuple[int, ...]]:
        """Return (fully_covers, missing_codepoints)."""
        missing = tuple({ord(ch) for ch in text if not self.covers_codepoint(ord(ch))})
        return (not missing, missing)

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.family, "subfamily": self.subfamily, "version": self.version,
            "file_hash": self.file_hash, "file_uri": self.file_uri, "license": self.license.value,
            "formats": list(self.formats), "coverage_blocks": sorted(self.coverage_blocks),
            "explicit_codepoints": sorted(self.explicit_codepoints),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "FontEntry":
        try:
            license_ = FontLicense(data.get("license", "missing"))  # type: ignore[arg-type]
        except ValueError:
            license_ = FontLicense.MISSING
        return cls(
            family=str(data.get("family", "")), subfamily=str(data.get("subfamily", "Regular")),
            version=str(data.get("version", "1.0")), file_hash=str(data.get("file_hash", "")),
            file_uri=str(data.get("file_uri", "")), license=license_,
            formats=tuple(str(x) for x in data.get("formats", ("ttf",))),
            coverage_blocks=frozenset(str(x) for x in data.get("coverage_blocks", [])),
            explicit_codepoints=frozenset(int(x) for x in data.get("explicit_codepoints", [])),
        )


@dataclass(frozen=True)
class FontResolution:
    """Result of resolving a fallback chain against some text."""

    family: str
    chosen: FontEntry | None
    missing_by_font: Mapping[str, tuple[int, ...]]
    blocks_delivery: bool


class FontRegistry:
    """Pinned family registry + fallback-chain resolution."""

    def __init__(self) -> None:
        self._by_key: dict[str, FontEntry] = {}

    def register(self, entry: FontEntry) -> None:
        self._by_key[entry.key] = entry

    def resolve_family(self, family: str, subfamily: str = "Regular") -> FontEntry | None:
        return self._by_key.get(f"{family}/{subfamily}") or self._by_key.get(f"{family}/Regular")

    def resolve_chain(self, text: str, families: Iterable[str]) -> FontResolution:
        """Resolve a fallback chain: assign each char to the first covering font.

        Reports missing codepoints per font and whether license blocks delivery.
        """
        families = list(families)
        chosen: FontEntry | None = None
        missing_by_font: dict[str, tuple[int, ...]] = {}
        text_codepoints = {ord(ch) for ch in text}
        unresolved = set(text_codepoints)
        contributor_blocked = False
        for family in families:
            entry = self.resolve_family(family)
            if entry is None:
                continue
            if chosen is None:
                chosen = entry
            covers = {cp for cp in text_codepoints if entry.covers_codepoint(cp)}
            missing_by_font[family] = tuple(sorted(text_codepoints - covers))
            contributed = covers & unresolved
            unresolved -= covers   # a codepoint is unresolved only if NO font covers it
            # AUD-27505: every font that actually supplies glyphs to the
            # rendered text participates in the delivery — a license-restricted
            # fallback (PREVIEW_ONLY/MISSING) must block delivery even when the
            # first resolvable font in the chain is openly licensed.
            if contributed and entry.license not in (FontLicense.OPEN, FontLicense.EMBEDDED):
                contributor_blocked = True
        # Anything still unresolved after the whole chain is a true missing glyph.
        if unresolved:
            missing_by_font["__unresolved__"] = tuple(sorted(unresolved))
        blocks = bool(
            chosen is not None
            and chosen.license in (FontLicense.OPEN, FontLicense.EMBEDDED)
            and not contributor_blocked
        )
        return FontResolution(
            family=families[0] if families else "", chosen=chosen,
            missing_by_font=missing_by_font, blocks_delivery=not blocks,
        )


__all__ = [
    "FontEntry", "FontLicense", "FontRegistry", "FontResolution", "GLYPH_BLOCKS",
]
