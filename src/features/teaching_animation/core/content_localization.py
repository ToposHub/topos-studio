"""Locale resolution for user-authored MG content (templates, presets).

Manifest ``*_i18n`` fields let one package ship both ``zh_CN`` and ``en_US``
display text while keeping a language-neutral fallback string.  Resolution
order: exact current locale → ``en_US`` → the manifest's base string, so a
missing translation degrades for that item only and never blanks a panel.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.shared.i18n.i18n import I18N


def localized_text(base: str, i18n_values: Mapping[str, Any] | None) -> str:
    """Resolve one manifest text against the current UI locale."""
    if not i18n_values:
        return base
    locale = I18N.get_current_locale()
    for candidate in (locale, "en_US"):
        value = i18n_values.get(candidate)
        if isinstance(value, str) and value.strip():
            return value
    return base


def sanitized_i18n_dict(value: Any) -> dict[str, str]:
    """Normalize a manifest ``*_i18n`` field to ``{locale: text}`` strings."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(locale): str(text).strip()
        for locale, text in value.items()
        if isinstance(locale, str) and locale and isinstance(text, str) and text.strip()
    }


def template_display_name(template: Any) -> str:
    """Localized display name of an HtmlTemplate (``display_name_i18n`` metadata)."""
    metadata = getattr(template, "metadata", None)
    if isinstance(metadata, Mapping):
        return localized_text(
            str(getattr(template, "display_name", "") or ""),
            metadata.get("display_name_i18n"),
        )
    return str(getattr(template, "display_name", "") or "")


def template_description(template: Any) -> str:
    """Localized description of an HtmlTemplate (``description_i18n`` metadata)."""
    metadata = getattr(template, "metadata", None)
    if isinstance(metadata, Mapping):
        return localized_text(
            str(getattr(template, "description", "") or ""),
            metadata.get("description_i18n"),
        )
    return str(getattr(template, "description", "") or "")


__all__ = [
    "localized_text",
    "sanitized_i18n_dict",
    "template_description",
    "template_display_name",
]
