"""Project-level quality profiles for composite evaluation.

``proxy_scale`` remains the concrete resolution control used by the existing
renderer. This module adds a stable semantic label around it so a request,
cache entry, diagnostic report, and future project setting can distinguish a
draft preview from a normal preview and a final render.
"""

from __future__ import annotations

from enum import Enum


class CompositeQualityProfile(str, Enum):
    """Semantic quality profiles shared by preview and export requests."""

    DRAFT = "draft"
    PREVIEW = "preview"
    FINAL = "final"


def normalize_quality_profile(value: object) -> str:
    """Return a supported profile name, falling back to ``preview``."""

    if isinstance(value, CompositeQualityProfile):
        return value.value
    normalized = str(value or "").strip().lower()
    if normalized in {profile.value for profile in CompositeQualityProfile}:
        return normalized
    return CompositeQualityProfile.PREVIEW.value


def quality_profile_for_request(render_mode: object = "preview",
                                proxy_scale: object = 1.0) -> str:
    """Resolve the semantic profile for a render request.

    Full/export requests are always final quality. Preview requests using a
    reduced proxy scale are draft quality; full-resolution preview remains the
    normal preview profile. This function deliberately does not mutate either
    input, so callers can use it in request construction and cache-key code.
    """

    mode = str(render_mode or "preview").strip().lower()
    if mode in {"full", "export", "final"}:
        return CompositeQualityProfile.FINAL.value
    try:
        scale = float(proxy_scale)
    except (TypeError, ValueError):
        scale = 1.0
    return (
        CompositeQualityProfile.DRAFT.value
        if scale < 0.999
        else CompositeQualityProfile.PREVIEW.value
    )


__all__ = [
    "CompositeQualityProfile",
    "normalize_quality_profile",
    "quality_profile_for_request",
]
