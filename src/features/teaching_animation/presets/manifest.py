"""Preset manifest (``preset.json``) parsing and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

PRESET_MANIFEST_SCHEMA_VERSION = 1
PRESET_ENTRY_CODE = "build.py"
PRESET_ENTRY_SCENE = "scene.json"
PRESET_PREVIEW_NAME = "preview.gif"
_PRESET_ENTRIES = frozenset({PRESET_ENTRY_CODE, PRESET_ENTRY_SCENE})

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class PresetError(ValueError):
    """Raised when a preset package is malformed or fails validation."""


def slugify(value: Any, *, fallback: str = "preset") -> str:
    """Lowercase identifier safe for folder names on every platform."""
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    return text or fallback


def normalize_category(value: Any) -> str:
    text = slugify(value, fallback="")
    if not _SLUG_RE.match(text):
        raise PresetError(f"invalid preset category slug: {value!r}")
    return text


def normalize_preview_path(value: Any) -> str:
    """Validate ``preview`` as a plain path anchored inside the preset folder.

    AUD-29705: every preview read/write joins the manifest value verbatim
    (``folder / manifest.preview``), so an absolute path or any ``..``
    component would render the GIF outside the preset library and overwrite
    an arbitrary file.  The manifest does not know its folder, so anchoring
    is enforced by rejecting everything that is not a plain in-folder
    relative path: absolute POSIX/Windows paths (including UNC shares and
    drive-relative ``C:x``), backslash separators, and ``..``/``.``/empty
    components.  With those rejected, ``folder / preview`` can never escape
    ``folder`` — the is_relative_to guarantee expressed without a base.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise PresetError("preset preview path cannot be empty")
    if "\\" in text:
        raise PresetError(f"preset preview path must use '/' separators: {value!r}")
    windows = PureWindowsPath(text)
    if PurePosixPath(text).is_absolute() or windows.is_absolute() or windows.drive:
        raise PresetError(f"preset preview path must be relative: {value!r}")
    parts = PurePosixPath(text).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise PresetError(f"preset preview path must stay inside the preset folder: {value!r}")
    return text


@dataclass(frozen=True)
class PresetManifest:
    """One preset's identity and entry points inside its folder."""

    preset_id: str
    name: str
    category: str
    description: str = ""
    tags: tuple[str, ...] = ()
    entry: str = PRESET_ENTRY_SCENE
    preview: str = PRESET_PREVIEW_NAME
    schema_version: int = PRESET_MANIFEST_SCHEMA_VERSION
    # Optional per-locale display text (``{"en_US": ..., "zh_CN": ...}``);
    # resolved at UI time, base ``name``/``description`` remain the fallback.
    name_i18n: tuple[tuple[str, str], ...] = ()
    description_i18n: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.preset_id or ""):
            raise PresetError(f"invalid preset_id slug: {self.preset_id!r}")
        if not str(self.name).strip():
            raise PresetError("preset name cannot be empty")
        if self.entry not in _PRESET_ENTRIES:
            raise PresetError(f"unsupported preset entry: {self.entry!r}")
        try:
            object.__setattr__(self, "category", normalize_category(self.category))
        except PresetError:
            raise PresetError(f"invalid category in manifest: {self.category!r}") from None
        # AUD-29704: a non-iterable ``tags`` (or a str() failure inside it)
        # must surface as PresetError, never as a bare TypeError that escapes
        # the scanner's per-preset PresetError handling and kills the scan.
        try:
            normalized_tags = tuple(str(tag).strip() for tag in self.tags if str(tag).strip())
        except (TypeError, ValueError) as exc:
            raise PresetError(f"invalid tags in manifest: {exc}") from None
        object.__setattr__(self, "tags", normalized_tags)
        # AUD-29705: preview joins the preset folder verbatim on disk, so it
        # must be a plain in-folder relative path (see normalize_preview_path).
        try:
            object.__setattr__(self, "preview", normalize_preview_path(self.preview))
        except PresetError:
            raise PresetError(f"invalid preview path in manifest: {self.preview!r}") from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preset_id": self.preset_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "tags": list(self.tags),
            "entry": self.entry,
            "preview": self.preview,
            "name_i18n": dict(self.name_i18n),
            "description_i18n": dict(self.description_i18n),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PresetManifest":
        # AUD-29704: tags/schema_version conversion errors (TypeError from a
        # non-iterable tags value, ValueError from a non-integer schema
        # version) used to escape as their builtin types and crash the whole
        # preset scan, whose per-folder degradation only catches PresetError.
        tags_raw = data.get("tags", ())
        if isinstance(tags_raw, (str, bytes)):
            tags_raw = (tags_raw,)  # a lone string is one tag, not its chars
        try:
            tags = tuple(tags_raw)
        except (TypeError, ValueError) as exc:
            raise PresetError(f"invalid tags in manifest: {exc}") from None
        try:
            schema_version = int(data.get("schema_version", PRESET_MANIFEST_SCHEMA_VERSION))
        except (TypeError, ValueError) as exc:
            raise PresetError(f"invalid schema_version in manifest: {exc}") from None
        return cls(
            preset_id=str(data.get("preset_id", "")).strip(),
            name=str(data.get("name", "")).strip(),
            category=str(data.get("category", "general")).strip(),
            description=str(data.get("description", "")),
            tags=tags,
            entry=str(data.get("entry", PRESET_ENTRY_SCENE)),
            preview=str(data.get("preview", PRESET_PREVIEW_NAME)),
            schema_version=schema_version,
            name_i18n=_i18n_pairs(data.get("name_i18n")),
            description_i18n=_i18n_pairs(data.get("description_i18n")),
        )


def _i18n_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    from .i18n_fields import sanitized_i18n_mapping
    return tuple(sanitized_i18n_mapping(value).items())


def localized_preset_name(manifest: "PresetManifest") -> str:
    """Locale-resolved preset name with graceful base-string fallback."""
    from ..core.content_localization import localized_text
    return localized_text(manifest.name, dict(manifest.name_i18n))


def localized_preset_description(manifest: "PresetManifest") -> str:
    """Locale-resolved preset description with graceful fallback."""
    from ..core.content_localization import localized_text
    return localized_text(manifest.description, dict(manifest.description_i18n))


__all__ = [
    "PRESET_ENTRY_CODE", "PRESET_ENTRY_SCENE", "PRESET_MANIFEST_SCHEMA_VERSION",
    "PRESET_PREVIEW_NAME", "PresetError", "PresetManifest", "localized_preset_description",
    "localized_preset_name", "normalize_category", "normalize_preview_path", "slugify",
]
