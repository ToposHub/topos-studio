"""i18n helpers for the presets package (avoids a core/UI import cycle)."""

from __future__ import annotations

from typing import Any, Mapping


def sanitized_i18n_mapping(value: Any) -> dict[str, str]:
    """Normalize a manifest ``*_i18n`` field to ``{locale: text}`` strings."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(locale): str(text).strip()
        for locale, text in value.items()
        if isinstance(locale, str) and locale and isinstance(text, str) and text.strip()
    }


__all__ = ["sanitized_i18n_mapping"]
